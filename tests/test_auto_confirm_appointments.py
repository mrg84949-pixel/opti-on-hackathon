from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Appointment, AppointmentStatus, Organization
from bot.llm.booking_draft import PENDING_BOOKING_KEY
from bot.llm.context import TurnContext
from bot.llm.tools import DIALOG_MODE_MANAGE, LAST_APPOINTMENT_ID_KEY, tool_by_name
from bot.services import appointment_service
from web.admin_auth import AdminAuth, get_admin_auth


class _FakeExecuteResult:
    def __init__(self, scalar_one_or_none_value=None):
        self._scalar_one_or_none_value = scalar_one_or_none_value

    def scalar_one_or_none(self):
        return self._scalar_one_or_none_value

    def scalars(self):
        return self

    def first(self):
        return None


class _FakeSession:
    def __init__(self, *, org=None, customer=None, conflict=None):
        self.org = org
        self.customer = customer
        self.conflict = conflict
        self.added = []
        self.flushed = False

    async def get(self, model, key):
        model_name = getattr(model, "__name__", "")
        if model_name == "Organization":
            return self.org
        if model_name == "Customer":
            return self.customer
        return None

    async def execute(self, _stmt):
        return _FakeExecuteResult(self.conflict)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = 99
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        return None

    async def refresh(self, _obj):
        return None


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSessionManagerOrg:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSessionOrg:
    def __init__(self, *, org: Organization):
        self._org = org
        self.committed = False
        self.refreshed = False

    async def get(self, model, key):
        if model is Organization and self._org.id == key:
            return self._org
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        self.refreshed = True


def _customer(*, dialog_context=None):
    return SimpleNamespace(
        id=10,
        name="Alice",
        phone="tg:123",
        dialog_context=dialog_context or {},
    )


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers(*, org_id: int = 1) -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": str(org_id)}


@pytest.mark.asyncio
async def test_auto_confirm_if_enabled_false():
    org = SimpleNamespace(auto_confirm_appointments=False)
    appt = Appointment(
        customer_id=1,
        scheduled_at=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
        status=AppointmentStatus.NEW,
    )
    session = _FakeSession()
    confirmed = await appointment_service.auto_confirm_if_enabled(session, org, appt)
    assert confirmed is False
    assert appt.status == AppointmentStatus.NEW


@pytest.mark.asyncio
async def test_auto_confirm_if_enabled_true():
    org = SimpleNamespace(auto_confirm_appointments=True)
    appt = Appointment(
        customer_id=1,
        scheduled_at=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
        status=AppointmentStatus.NEW,
    )
    session = _FakeSession()

    confirmed = await appointment_service.auto_confirm_if_enabled(session, org, appt)
    assert confirmed is True
    assert appt.status == AppointmentStatus.CONFIRMED
    assert session.flushed is True


def _demo_org(**overrides):
    base = {
        "id": 1,
        "timezone": "UTC",
        "crm_provider": "none",
        "crm_base_url": None,
        "crm_api_token": None,
        "auto_confirm_appointments": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_crm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Provider:
        async def get_available_slots(self, **kwargs):
            return [
                SimpleNamespace(
                    start=datetime(2027, 6, 15, 10, 0),
                    end=datetime(2027, 6, 15, 10, 30),
                )
            ]

        async def book_appointment(self, **kwargs):
            return SimpleNamespace(crm_appointment_id="crm-1")

    monkeypatch.setattr("bot.services.appointment_service.get_crm_provider", lambda _org: _Provider())
    monkeypatch.setattr("bot.llm.tools.get_crm_provider", lambda _org: _Provider())


@pytest.mark.asyncio
async def test_confirm_booking_auto_confirm_notifies_client(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org(auto_confirm_appointments=True)
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = _customer(dialog_context={PENDING_BOOKING_KEY: draft})
    fake_session = _FakeSession(org=org, customer=customer)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    _patch_crm_provider(monkeypatch)

    called = {"admin": 0, "client": 0}

    async def fake_notify_new(**_kwargs):
        called["admin"] += 1
        return 1

    async def fake_send_client(_org, _customer, _text):
        called["client"] += 1
        return True

    monkeypatch.setattr("bot.llm.tools.notify_admins_about_new_appointment", fake_notify_new)
    monkeypatch.setattr("bot.llm.tools.notification_service.send_customer_message", fake_send_client)

    confirm = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "confirm_appointment_booking")
    result = await confirm()

    assert "подтверждена" in result
    assert called["admin"] == 0
    assert called["client"] == 1
    assert fake_session.added[0].status == AppointmentStatus.CONFIRMED


@pytest.mark.asyncio
async def test_confirm_booking_manual_unchanged(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org(auto_confirm_appointments=False)
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = _customer(dialog_context={PENDING_BOOKING_KEY: draft})
    fake_session = _FakeSession(org=org, customer=customer)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    _patch_crm_provider(monkeypatch)

    called = {"admin": 0, "client": 0}

    async def fake_notify_new(**_kwargs):
        called["admin"] += 1
        return 1

    async def fake_send_client(_org, _customer, _text):
        called["client"] += 1
        return True

    monkeypatch.setattr("bot.llm.tools.notify_admins_about_new_appointment", fake_notify_new)
    monkeypatch.setattr("bot.llm.tools.notification_service.send_customer_message", fake_send_client)

    confirm = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "confirm_appointment_booking")
    result = await confirm()

    assert "новая" in result
    assert called["admin"] == 1
    assert called["client"] == 0
    assert fake_session.added[0].status == AppointmentStatus.NEW


@pytest.mark.asyncio
async def test_patch_org_settings_auto_confirm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    org.auto_confirm_appointments = False
    fake_session = _FakeSessionOrg(org=org)

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=1)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManagerOrg(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch(
                "/api/web/org-settings",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
                json={"auto_confirm_appointments": True},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert org.auto_confirm_appointments is True
    assert response.json()["auto_confirm_appointments"] is True
    assert fake_session.committed is True

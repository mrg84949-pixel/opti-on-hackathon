from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.crm.base import CrmAppointmentSnapshot
from bot.db.models import Appointment, AppointmentStatus, Customer, Organization
from bot.services import crm_appointment_sync_service as sync_service


class _FakeSession:
    def __init__(self, *, org: Organization | None, appointment_rows: list | None = None):
        self._org = org
        self.committed = False
        self.flushed = False
        self._appointment_rows = appointment_rows or []

    async def get(self, model, key):
        if model is Organization and self._org is not None and self._org.id == key:
            return self._org
        return None

    async def execute(self, _stmt):
        return _FakeResult(self._appointment_rows)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


class _FakeResult:
    def __init__(self, rows: list):
        self._rows = rows

    def unique(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSessionManager:
    def __init__(self, session: _FakeSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _org(**kwargs) -> Organization:
    org = Organization(
        id=1,
        name="Clinic A",
        crm_provider="amocrm",
        crm_base_url="https://crm.example",
        crm_api_token="secret-token",
        timezone="UTC",
        billing_paid_until=datetime.now(timezone.utc) + timedelta(days=30),
    )
    for key, value in kwargs.items():
        setattr(org, key, value)
    return org


def _appointment(*, org: Organization, crm_id: str = "crm-99") -> Appointment:
    customer = Customer(
        id=5,
        org_id=org.id,
        name="Ali",
        phone="tg:12345",
        organization=org,
    )
    appt = Appointment(
        id=10,
        customer_id=customer.id,
        scheduled_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        status=AppointmentStatus.CONFIRMED,
        crm_appointment_id=crm_id,
        customer=customer,
    )
    return appt


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers(*, org_id: int = 1) -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": str(org_id)}


@pytest.mark.asyncio
async def test_sync_org_crm_cancellations_cancels_and_notifies(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    appt = _appointment(org=org)
    session = _FakeSession(org=org)

    class _FakeProvider:
        demo_mode = False
        cancelled_status_value = "cancelled"

        async def list_recent_appointments(self, *, since_iso: str):
            assert since_iso
            return [CrmAppointmentSnapshot(crm_appointment_id="crm-99", status="cancelled")]

    monkeypatch.setattr(sync_service, "get_crm_provider", lambda _org: _FakeProvider())
    monkeypatch.setattr(
        sync_service,
        "get_appointment_by_crm_id",
        AsyncMock(return_value=appt),
    )
    notify = AsyncMock()
    monkeypatch.setattr(sync_service.notification_service, "send_customer_message", notify)

    async def _fake_cancel(_session, org_id, appt_id, reason):
        appt.status = AppointmentStatus.CANCELLED
        appt.cancel_reason = reason
        return {
            "scheduled_at": appt.scheduled_at.isoformat(),
            "cancel_reason": reason,
            "status": "cancelled",
        }, appt.customer

    monkeypatch.setattr(sync_service.appointment_service, "cancel_appointment", _fake_cancel)

    result = await sync_service.sync_org_crm_cancellations(session, 1)
    assert result["synced"] == 1
    assert appt.status == AppointmentStatus.CANCELLED
    assert appt.cancel_reason == sync_service.CRM_CANCEL_REASON
    notify.assert_awaited_once()
    assert org.crm_config and org.crm_config.get(sync_service.CRM_SYNC_WATERMARK_KEY)


@pytest.mark.asyncio
async def test_sync_org_skips_already_cancelled(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    appt = _appointment(org=org)
    appt.status = AppointmentStatus.CANCELLED
    session = _FakeSession(org=org)

    class _FakeProvider:
        demo_mode = False
        cancelled_status_value = "cancelled"

        async def list_recent_appointments(self, *, since_iso: str):
            return [CrmAppointmentSnapshot(crm_appointment_id="crm-99", status="cancelled")]

    monkeypatch.setattr(sync_service, "get_crm_provider", lambda _org: _FakeProvider())
    monkeypatch.setattr(
        sync_service,
        "get_appointment_by_crm_id",
        AsyncMock(return_value=appt),
    )
    notify = AsyncMock()
    monkeypatch.setattr(sync_service.notification_service, "send_customer_message", notify)

    result = await sync_service.sync_org_crm_cancellations(session, 1)

    assert result["synced"] == 0
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_org_skips_demo_crm():
    org = _org(crm_provider="demo", crm_base_url=None, crm_api_token=None)
    session = _FakeSession(org=org)

    result = await sync_service.sync_org_crm_cancellations(session, 1)

    assert result["skipped"] is True
    assert result["reason"] == "crm_not_configured"


@pytest.mark.asyncio
async def test_sync_org_yclients_eligible_without_base_url(monkeypatch: pytest.MonkeyPatch):
    org = _org(
        crm_provider="yclients",
        crm_base_url=None,
        crm_api_token="partner",
        crm_user_token="user",
        crm_config={"company_id": "4564"},
    )
    session = _FakeSession(org=org)

    class _FakeProvider:
        demo_mode = False
        cancelled_status_value = "cancelled"

        async def list_recent_appointments(self, *, since_iso: str):
            return []

    monkeypatch.setattr(sync_service, "get_crm_provider", lambda _org: _FakeProvider())

    result = await sync_service.sync_org_crm_cancellations(session, 1)

    assert result["skipped"] is False
    assert result["synced"] == 0


@pytest.mark.asyncio
async def test_sync_org_yclients_skipped_without_user_token():
    org = _org(
        crm_provider="yclients",
        crm_base_url=None,
        crm_api_token="partner",
        crm_config={"company_id": "4564"},
    )
    session = _FakeSession(org=org)

    result = await sync_service.sync_org_crm_cancellations(session, 1)

    assert result["skipped"] is True
    assert result["reason"] == "crm_not_configured"


@pytest.mark.asyncio
async def test_sync_org_macdent_is_a_noop_handled_via_webhook(monkeypatch: pytest.MonkeyPatch):
    """MacDent bookings are real zapis records now (book_appointment ->
    zapis.add, confirmed live 2026-08-29) and ARE covered by MacDent's own
    webhook events — cancellations arrive in real time via
    bot/api/macdent_webhook.py instead of being polled here. zapis.find's
    date filter is also confirmed non-functional, so there is no safe
    polling fallback either — this path must stay a clean no-op, not
    silently call list_recent_appointments (which would page the CRM's
    entire history) or any narrow-poll method."""
    org = _org(crm_provider="macdent", crm_base_url=None, crm_api_token="mac-token")
    session = _FakeSession(org=org)

    list_recent_called = False

    class _FakeMacDentProvider:
        demo_mode = False

        async def list_recent_appointments(self, *, since_iso: str):
            nonlocal list_recent_called
            list_recent_called = True
            return []

    monkeypatch.setattr(sync_service, "get_crm_provider", lambda _org: _FakeMacDentProvider())

    result = await sync_service.sync_org_crm_cancellations(session, 1)

    assert list_recent_called is False
    assert result["skipped"] is True
    assert result["reason"] == "handled_via_webhook"
    assert result["synced"] == 0


@pytest.mark.asyncio
async def test_apply_crm_reschedule_updates_time_and_notifies(monkeypatch: pytest.MonkeyPatch):
    org = _org(crm_provider="macdent", crm_base_url=None, crm_api_token="mac-token")
    appt = _appointment(org=org, crm_id="mac-appt-9")
    session = _FakeSession(org=org)

    notify = AsyncMock()
    monkeypatch.setattr(sync_service.notification_service, "send_customer_message", notify)

    new_time = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    outcome = await sync_service.apply_crm_reschedule(session, org, 1, appt, new_time)

    assert outcome == "synced"
    assert appt.scheduled_at == new_time
    assert appt.reminder_24h_sent_at is None
    assert appt.reminder_2h_sent_at is None
    assert appt.status == AppointmentStatus.CONFIRMED  # unchanged — no forced re-confirmation
    notify.assert_awaited_once()
    assert session.flushed is True


@pytest.mark.asyncio
async def test_apply_crm_reschedule_skips_when_time_unchanged(monkeypatch: pytest.MonkeyPatch):
    org = _org(crm_provider="macdent", crm_base_url=None, crm_api_token="mac-token")
    appt = _appointment(org=org, crm_id="mac-appt-10")
    session = _FakeSession(org=org)

    notify = AsyncMock()
    monkeypatch.setattr(sync_service.notification_service, "send_customer_message", notify)

    outcome = await sync_service.apply_crm_reschedule(session, org, 1, appt, appt.scheduled_at)

    assert outcome == "skipped"
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_crm_reschedule_skips_cancelled_appointment(monkeypatch: pytest.MonkeyPatch):
    org = _org(crm_provider="macdent", crm_base_url=None, crm_api_token="mac-token")
    appt = _appointment(org=org, crm_id="mac-appt-11")
    appt.status = AppointmentStatus.CANCELLED
    session = _FakeSession(org=org)

    notify = AsyncMock()
    monkeypatch.setattr(sync_service.notification_service, "send_customer_message", notify)

    new_time = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    outcome = await sync_service.apply_crm_reschedule(session, org, 1, appt, new_time)

    assert outcome == "skipped"
    notify.assert_not_awaited()
    assert appt.scheduled_at != new_time


@pytest.mark.asyncio
async def test_crm_appointments_sync_api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))
    monkeypatch.setattr(
        admin_api.crm_appointment_sync_service,
        "sync_org_crm_cancellations",
        AsyncMock(return_value={"org_id": 1, "skipped": False, "synced": 0, "errors": 0}),
    )

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/crm/appointments/sync", headers=_auth_headers())

    assert response.status_code == 200
    assert response.json()["org_id"] == 1
    assert session.committed is True

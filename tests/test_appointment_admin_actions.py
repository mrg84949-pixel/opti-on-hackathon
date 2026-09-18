from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.services.outbound_result import OutboundSendResult


def _auth_headers():
    return {"Authorization": "Bearer 1234", "x-org-id": "1"}


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeSession:
    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True

    async def flush(self):
        pass

    async def get(self, model, key):
        # Soft paywall opens a short-lived session before appointment actions.
        if model is admin_api.Organization or getattr(model, "__name__", None) == "Organization":
            return SimpleNamespace(id=key, billing_paid_until=None, bot_enabled=True, name="Demo")
        return None

    async def execute(self, _stmt):
        class _Result:
            def scalars(self):
                return self

            def first(self):
                return None

        return _Result()


def _test_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _customer_with_org():
    org = SimpleNamespace(id=1, name="Demo")
    return SimpleNamespace(id=2, name="Aruzhan", phone="tg:123", org_id=1, organization=org)


@pytest.mark.asyncio
async def test_confirm_appointment_route(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession()
    item = {
        "id": 11,
        "customer_id": 2,
        "customer_name": "Aruzhan",
        "customer_phone": "tg:123",
        "scheduled_at": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).isoformat(),
        "status": "confirmed",
        "crm_appointment_id": None,
        "crm_doctor_id": None,
        "reminder_24h_sent_at": None,
        "cancel_reason": None,
    }
    customer = _customer_with_org()

    async def fake_confirm(session, org_id, appt_id):
        assert org_id == 1
        assert appt_id == 11
        return item, customer

    async def fake_send(org, cust, text):
        assert "подтверждена" in text.lower()
        return OutboundSendResult.success(channel="telegram")

    monkeypatch.setattr(admin_api.appointment_service, "confirm_appointment", fake_confirm)
    monkeypatch.setattr(admin_api.notification_service, "send_customer_message", fake_send)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/appointments/11/confirm", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["item"]["status"] == "confirmed"
    assert payload["notification_sent"] is True
    assert fake_session.committed is True


class _FakeSessionWithOrg(_FakeSession):
    def __init__(self, org):
        super().__init__()
        self.org = org

    async def get(self, _model, key):
        if key == 1:
            return self.org
        return None


@pytest.mark.asyncio
async def test_propose_change_route(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    local_dt = datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)
    customer = _customer_with_org()
    customer.organization.timezone = "UTC"
    fake_session = _FakeSessionWithOrg(customer.organization)
    item = {
        "id": 11,
        "customer_id": 2,
        "customer_name": "Aruzhan",
        "customer_phone": "tg:123",
        "scheduled_at": local_dt.isoformat(),
        "status": "confirmed",
        "crm_appointment_id": None,
        "crm_doctor_id": "doc-1",
        "reminder_24h_sent_at": None,
        "cancel_reason": None,
        "client_change_reason": "Перенос",
        "client_change_deadline_at": local_dt.isoformat(),
        "client_change_requested_at": local_dt.isoformat(),
    }

    async def fake_propose(session, org_id, appt_id, **kwargs):
        assert kwargs["reason"] == "Перенос"
        return item, customer, local_dt

    async def fake_send(org, cust, text):
        assert "изменил" in text.lower() or "да" in text.lower()
        return OutboundSendResult.success(channel="telegram")

    monkeypatch.setattr(
        admin_api.appointment_service, "propose_appointment_change", fake_propose
    )
    monkeypatch.setattr(admin_api.notification_service, "send_customer_message", fake_send)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/appointments/11/propose-change",
            headers=_auth_headers(),
            json={"date": "2026-06-02", "time": "15:00", "reason": "Перенос"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["notification_sent"] is True
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_cancel_appointment_route_validation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/appointments/11/cancel",
            headers=_auth_headers(),
            json={"reason": "no"},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_cancel_appointment_route_conflict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession()

    async def fake_cancel(session, org_id, appt_id, reason):
        raise admin_api.appointment_service.InvalidStatusTransitionError("bad transition")

    monkeypatch.setattr(admin_api.appointment_service, "cancel_appointment", fake_cancel)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/appointments/11/cancel",
            headers=_auth_headers(),
            json={"reason": "Клиент не пришёл"},
        )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_complete_appointment_includes_2gis_link(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession()
    item = {
        "id": 11,
        "customer_id": 2,
        "customer_name": "Aruzhan",
        "customer_phone": "tg:123",
        "scheduled_at": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).isoformat(),
        "status": "completed",
        "crm_appointment_id": None,
        "crm_doctor_id": None,
        "reminder_24h_sent_at": None,
        "cancel_reason": None,
    }
    customer = _customer_with_org()
    customer.organization.review_2gis_url = "https://2gis.ru/firm/42"
    sent_text: list[str] = []

    async def fake_complete(session, org_id, appt_id):
        return item, customer

    async def fake_send(org, cust, text):
        sent_text.append(text)
        return OutboundSendResult.success(channel="telegram")

    monkeypatch.setattr(admin_api.appointment_service, "complete_appointment", fake_complete)
    monkeypatch.setattr(admin_api.notification_service, "send_customer_message", fake_send)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/appointments/11/complete", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json()["notification_sent"] is True
    assert len(sent_text) == 1
    assert "2ГИС" in sent_text[0]
    assert "https://2gis.ru/firm/42" in sent_text[0]


@pytest.mark.asyncio
async def test_complete_appointment_includes_care_and_upsell(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession()
    item = {
        "id": 11,
        "customer_id": 2,
        "customer_name": "Aruzhan",
        "customer_phone": "tg:123",
        "scheduled_at": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).isoformat(),
        "status": "completed",
        "crm_appointment_id": None,
        "crm_doctor_id": None,
        "reminder_24h_sent_at": None,
        "cancel_reason": None,
        "service_name": "Консультация",
    }
    customer = _customer_with_org()
    customer.organization.post_service_upsell_message = "Ждём вас в {org_name}!"
    sent_text: list[str] = []

    async def fake_complete(session, org_id, appt_id):
        return item, customer

    async def fake_resolve_care(_session, _org_id, service_name):
        assert service_name == "Консультация"
        return "Не есть 2 часа."

    async def fake_send(org, cust, text):
        sent_text.append(text)
        return OutboundSendResult.success(channel="telegram")

    monkeypatch.setattr(admin_api.appointment_service, "complete_appointment", fake_complete)
    monkeypatch.setattr(admin_api.org_services_catalog, "resolve_care_message", fake_resolve_care)
    monkeypatch.setattr(admin_api.notification_service, "send_customer_message", fake_send)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/appointments/11/complete", headers=_auth_headers())
    assert response.status_code == 200
    assert len(sent_text) == 1
    assert "Совет по уходу:" in sent_text[0]
    assert "Не есть 2 часа." in sent_text[0]
    assert "Ждём вас в Demo!" in sent_text[0]


@pytest.mark.asyncio
async def test_complete_appointment_notification_failed_still_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession()
    item = {
        "id": 11,
        "customer_id": 2,
        "customer_name": "Aruzhan",
        "customer_phone": "plain-phone",
        "scheduled_at": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).isoformat(),
        "status": "completed",
        "crm_appointment_id": None,
        "crm_doctor_id": None,
        "reminder_24h_sent_at": None,
        "cancel_reason": None,
    }
    customer = _customer_with_org()
    customer.phone = "plain-phone"

    async def fake_complete(session, org_id, appt_id):
        return item, customer

    async def fake_send(org, cust, text):
        return OutboundSendResult.skipped(channel="telegram")

    monkeypatch.setattr(admin_api.appointment_service, "complete_appointment", fake_complete)
    monkeypatch.setattr(admin_api.notification_service, "send_customer_message", fake_send)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/appointments/11/complete", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json()["notification_sent"] is False
    assert fake_session.committed is True

"""Wave 2 C.3: CRM demo staff + cancel sync (API, notify, job)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.automation import crm_appointment_sync as sync_job
from bot.crm.base import CrmAppointmentSnapshot
from bot.db.models import Appointment, AppointmentStatus, Customer, Organization
from bot.services.outbound_result import OutboundSendResult
from test_appointment_admin_actions import _FakeSessionManager, _test_app


class _StaffFakeSession:
    def __init__(self, org):
        self.org = org
        self.committed = False

    async def get(self, model, key):
        if model is admin_api.Organization and key == self.org.id:
            return self.org
        return None

    async def commit(self):
        self.committed = True


class _SyncFakeSession:
    def __init__(self, org: Organization):
        self._org = org
        self.committed = False
        self.flushed = False

    async def get(self, model, key):
        if model is Organization and self._org is not None and self._org.id == key:
            return self._org
        return None

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


def _auth_headers(*, org_id: int = 1) -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": str(org_id)}


def _amocrm_org(**kwargs) -> Organization:
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


def _confirmed_appt(*, org: Organization, crm_id: str = "crm-99") -> Appointment:
    customer = Customer(
        id=5,
        org_id=org.id,
        name="Ali",
        phone="tg:12345",
        organization=org,
    )
    return Appointment(
        id=10,
        customer_id=customer.id,
        scheduled_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        status=AppointmentStatus.CONFIRMED,
        crm_appointment_id=crm_id,
        customer=customer,
    )


@pytest.mark.asyncio
async def test_c3_demo_staff_list_and_sync_routes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(
        id=1,
        name="Demo",
        crm_provider="none",
        crm_base_url=None,
        crm_api_token=None,
    )
    fake_session = _StaffFakeSession(org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        list_resp = await client.get("/api/web/crm/staff", headers=_auth_headers())
        sync_resp = await client.post("/api/web/crm/staff/sync", headers=_auth_headers())

    assert list_resp.status_code == 200
    list_payload = list_resp.json()
    assert list_payload["source"] == "demo"
    assert len(list_payload["items"]) >= 2

    assert sync_resp.status_code == 200
    sync_payload = sync_resp.json()
    assert sync_payload["source"] == "demo"
    assert sync_payload["count"] >= 2
    assert sync_payload["synced_at"] is not None


@pytest.mark.asyncio
async def test_c3_crm_cancel_sync_notifies_via_api(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _amocrm_org()
    appt = _confirmed_appt(org=org)
    session = _SyncFakeSession(org=org)
    sent: list[str] = []

    class _FakeProvider:
        demo_mode = False
        cancelled_status_value = "cancelled"

        async def list_recent_appointments(self, *, since_iso: str):
            assert since_iso
            return [CrmAppointmentSnapshot(crm_appointment_id="crm-99", status="cancelled")]

    async def spy_send(_org, _cust, text):
        sent.append(text)
        return OutboundSendResult.success(channel="telegram")

    async def _fake_cancel(_session, org_id, appt_id, reason):
        appt.status = AppointmentStatus.CANCELLED
        appt.cancel_reason = reason
        return {
            "scheduled_at": appt.scheduled_at.isoformat(),
            "cancel_reason": reason,
            "status": "cancelled",
        }, appt.customer

    svc = admin_api.crm_appointment_sync_service
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))
    monkeypatch.setattr(svc, "get_crm_provider", lambda _org: _FakeProvider())
    monkeypatch.setattr(svc, "get_appointment_by_crm_id", AsyncMock(return_value=appt))
    monkeypatch.setattr(svc.appointment_service, "cancel_appointment", _fake_cancel)
    monkeypatch.setattr(svc.notification_service, "send_customer_message", spy_send)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/crm/appointments/sync", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["synced"] == 1
    assert appt.status == AppointmentStatus.CANCELLED
    assert session.committed is True
    assert len(sent) == 1
    assert "отменена" in sent[0].lower()


@pytest.mark.asyncio
async def test_c3_process_crm_sync_job_invokes_sync(monkeypatch: pytest.MonkeyPatch):
    from test_crm_appointment_sync_job import _FakeSession, _FakeSessionManager

    org = _amocrm_org()
    fake_session = _FakeSession([org])
    sync_mock = AsyncMock(return_value={"synced": 1})
    monkeypatch.setattr(sync_job, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    monkeypatch.setattr(sync_job, "_crm_sync_eligible", lambda _org: True)
    monkeypatch.setattr(sync_job, "sync_org_crm_cancellations", sync_mock)

    await sync_job.process_crm_appointment_sync()

    sync_mock.assert_awaited_once_with(fake_session, org.id)
    assert fake_session.committed is True

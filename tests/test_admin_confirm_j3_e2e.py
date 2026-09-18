"""Wave 2 C.1 J3: bot booking (NEW) → admin confirm → client TG notify."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from httpx import ASGITransport, AsyncClient
import pytest

import bot.llm.llm_engine as llm_engine
import web.admin_api as admin_api
from bot.db.models import Appointment, AppointmentStatus
from bot.services.outbound_result import OutboundSendResult
from test_appointment_admin_actions import _FakeSessionManager, _auth_headers, _test_app
from test_booking_edge_cases import _patch_fsm_edge_env
from test_bot_scenarios import (
    _FakeExecuteResult,
    _FakeSession,
    _customer,
    _org,
)

_BOOKING_STEPS = [
    "хочу записаться",
    "Тест J3",
    "Консультация",
    "2",
    "2027-06-15",
    "10",
    "да",
]


class _J3FakeSession(_FakeSession):
    """Fake session that supports appointment_service confirm lookup."""

    def __init__(self, *, org=None, customer=None, conflict=None):
        super().__init__(org=org, customer=customer, conflict=conflict)
        self._confirm_lookup: tuple[Appointment, object] | None = None

    def set_confirm_lookup(self, appt: Appointment, customer) -> None:
        customer.organization = self.org
        self._confirm_lookup = (appt, customer)

    async def execute(self, stmt):
        if self._confirm_lookup is not None:
            return _FakeExecuteResult(one_or_none_value=self._confirm_lookup)
        return await super().execute(stmt)


def _patch_j3_booking_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    org,
    customer,
    fake_session: _J3FakeSession,
) -> None:
    _patch_fsm_edge_env(
        monkeypatch,
        org=org,
        customer=customer,
        fake_session=fake_session,
    )
    org.auto_confirm_appointments = False


@pytest.mark.asyncio
async def test_j3_booking_admin_confirm_sends_notify(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(auto_confirm_appointments=False)
    customer = _customer()
    fake_session = _J3FakeSession(org=org, customer=customer)
    _patch_j3_booking_env(
        monkeypatch,
        org=org,
        customer=customer,
        fake_session=fake_session,
    )

    for text in _BOOKING_STEPS:
        await llm_engine.get_ai_response(
            "user-1",
            text,
            channel="web",
            org_id=org.id,
        )

    appts = [obj for obj in fake_session.added if isinstance(obj, Appointment)]
    assert len(appts) == 1
    appt = appts[0]
    assert appt.status == AppointmentStatus.NEW
    fake_session.set_confirm_lookup(appt, customer)

    sent: list[str] = []

    async def spy_send(org_obj, cust, text):
        sent.append(text)
        return OutboundSendResult.success(channel="telegram")

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    monkeypatch.setattr(admin_api.notification_service, "send_customer_message", spy_send)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/web/appointments/{appt.id}/confirm",
            headers=_auth_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["notification_sent"] is True
    assert payload["item"]["status"] == "confirmed"
    assert appt.status == AppointmentStatus.CONFIRMED
    assert len(sent) == 1
    assert "подтверждена" in sent[0].lower()


@pytest.mark.asyncio
async def test_j3_confirm_skipped_for_non_tg_phone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(id=1, name="Demo", timezone="UTC")
    customer = SimpleNamespace(
        id=10,
        org_id=1,
        name="Plain",
        phone="plain-phone",
        organization=org,
    )
    appt = Appointment(
        id=11,
        customer_id=10,
        scheduled_at=datetime(2027, 6, 15, 10, 0, tzinfo=timezone.utc),
        status=AppointmentStatus.NEW,
    )
    fake_session = _J3FakeSession(org=org, customer=customer)
    fake_session.set_confirm_lookup(appt, customer)

    async def spy_send(_org, _cust, _text):
        return OutboundSendResult.skipped(None)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    monkeypatch.setattr(admin_api.notification_service, "send_customer_message", spy_send)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/appointments/11/confirm",
            headers=_auth_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["notification_sent"] is False
    assert payload["item"]["status"] == "confirmed"
    assert appt.status == AppointmentStatus.CONFIRMED

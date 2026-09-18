"""Wave 2 C.2 J6: bot cancel/reschedule → client notify + admin cancel notify."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import bot.llm.llm_engine as llm_engine
from bot.db.models import AppointmentStatus
from bot.llm.scenarios import BOOKING_THEN_CANCEL, BOOKING_THEN_RESCHEDULE, BotScenario
from bot.llm.tools import (
    DIALOG_MODE_BOOKING,
    DIALOG_MODE_KEY,
    DIALOG_MODE_MANAGE,
    LAST_APPOINTMENT_ID_KEY,
    _notify_client_cancel as _real_notify_cancel,
    _notify_client_reschedule as _real_notify_reschedule,
)
from bot.services.outbound_result import OutboundSendResult
import web.admin_api as admin_api
from test_appointment_admin_actions import _FakeSession, _FakeSessionManager, _auth_headers, _test_app
from test_bot_scenarios import (
    _FakeSession,
    _customer,
    _org,
    _patch_bot_scenario_env,
    _sync_active_appointment,
)


def _patch_j6_notify_spy(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> None:
    async def spy_send(_org, _cust, text):
        sent.append(text)
        return OutboundSendResult.success(channel="telegram")

    monkeypatch.setattr("bot.llm.tools._notify_client_cancel", _real_notify_cancel)
    monkeypatch.setattr("bot.llm.tools._notify_client_reschedule", _real_notify_reschedule)
    monkeypatch.setattr(
        "bot.llm.tools.notification_service.send_customer_message",
        spy_send,
    )


async def _run_j6_scenario(
    monkeypatch: pytest.MonkeyPatch,
    scenario: BotScenario,
    *,
    sent: list[str],
):
    org = _org()
    customer = _customer()
    fake_session = _FakeSession(org=org, customer=customer)
    _patch_bot_scenario_env(
        monkeypatch,
        scenario=scenario,
        org=org,
        customer=customer,
        fake_session=fake_session,
    )
    _patch_j6_notify_spy(monkeypatch, sent)

    replies: list[str] = []
    for turn in scenario.turns:
        reply = await llm_engine.get_ai_response(
            "user-1",
            turn.user_text,
            channel="web",
            org_id=org.id,
        )
        replies.append(reply)
        _sync_active_appointment(fake_session, customer)
    return replies, fake_session, customer


@pytest.mark.asyncio
async def test_j6_booking_cancel_sends_notify(monkeypatch: pytest.MonkeyPatch):
    sent: list[str] = []
    replies, fake_session, customer = await _run_j6_scenario(
        monkeypatch, BOOKING_THEN_CANCEL, sent=sent
    )

    assert len(replies) == 3
    assert "отменена" in replies[2]
    assert fake_session.added[0].status == AppointmentStatus.CANCELLED
    assert customer.dialog_context.get(DIALOG_MODE_KEY) == DIALOG_MODE_BOOKING
    assert LAST_APPOINTMENT_ID_KEY not in customer.dialog_context
    assert len(sent) == 1
    assert "отменена" in sent[0].lower()


@pytest.mark.asyncio
async def test_j6_booking_reschedule_sends_notify(monkeypatch: pytest.MonkeyPatch):
    sent: list[str] = []
    replies, fake_session, customer = await _run_j6_scenario(
        monkeypatch, BOOKING_THEN_RESCHEDULE, sent=sent
    )

    assert len(replies) == 3
    assert "перенесена" in replies[2]
    appt = fake_session.added[0]
    assert appt.status == AppointmentStatus.NEW
    assert appt.scheduled_at == datetime(2027, 6, 2, 15, 0, tzinfo=timezone.utc)
    assert customer.dialog_context.get(DIALOG_MODE_KEY) == DIALOG_MODE_MANAGE
    assert customer.dialog_context.get(LAST_APPOINTMENT_ID_KEY) == 99
    assert len(sent) == 1
    assert "перенес" in sent[0].lower() or "15:00" in sent[0] or "02.06" in sent[0]


@pytest.mark.asyncio
async def test_j6_admin_cancel_sends_notify(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession(org=_org())
    item = {
        "id": 11,
        "customer_id": 2,
        "customer_name": "Aruzhan",
        "customer_phone": "tg:123",
        "scheduled_at": datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc).isoformat(),
        "status": "cancelled",
        "crm_appointment_id": None,
        "crm_doctor_id": None,
        "reminder_24h_sent_at": None,
        "cancel_reason": "Клиент не пришёл",
    }
    customer = SimpleNamespace(
        id=2,
        name="Aruzhan",
        phone="tg:123",
        org_id=1,
        organization=SimpleNamespace(id=1, name="Demo"),
    )
    sent: list[str] = []

    async def fake_cancel(session, org_id, appt_id, reason):
        assert org_id == 1
        assert appt_id == 11
        assert reason == "Клиент не пришёл"
        return item, customer

    async def fake_send(_org, _cust, text):
        sent.append(text)
        return OutboundSendResult.success(channel="telegram")

    monkeypatch.setattr(admin_api.appointment_service, "cancel_appointment", fake_cancel)
    monkeypatch.setattr(admin_api.notification_service, "send_customer_message", fake_send)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/appointments/11/cancel",
            headers=_auth_headers(),
            json={"reason": "Клиент не пришёл"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["notification_sent"] is True
    assert payload["item"]["status"] == "cancelled"
    assert len(sent) == 1
    assert "отменена" in sent[0].lower()

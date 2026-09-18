from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.llm.context import TurnContext
from bot.llm.tools import (
    DIALOG_MODE_BOOKING,
    DIALOG_MODE_KEY,
    DIALOG_MODE_MANAGE,
    make_tools,
    resolve_tool_mode,
    tool_by_name,
)


class _FakeSession:
    def __init__(self, *, org=None, customer=None):
        self.org = org
        self.customer = customer

    async def get(self, model, key):
        model_name = getattr(model, "__name__", "")
        if model_name == "Organization":
            return self.org
        if model_name == "Customer":
            return self.customer
        return None

    async def commit(self):
        return None


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_make_tools_manage_mode_names():
    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    names = [f.__name__ for f in make_tools(ctx, mode=DIALOG_MODE_MANAGE)]
    assert names == [
        "cancel_appointment",
        "respond_to_appointment_change",
        "edit_appointment",
        "list_crm_staff",
        "get_available_slots",
        "get_services_info",
        "get_customer_context",
        "compress_context",
        "disable_reminders",
        "transfer_to_human",
    ]
    booking_names = [f.__name__ for f in make_tools(ctx, mode=DIALOG_MODE_BOOKING)]
    assert "list_crm_staff" in booking_names
    assert "confirm_appointment_booking" in booking_names
    assert "disable_reminders" in booking_names
    assert "cancel_appointment" not in booking_names


@pytest.mark.asyncio
async def test_cancel_appointment_tool_notifies(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(id=1, timezone="Europe/Moscow")
    customer = SimpleNamespace(
        id=10,
        name="Ali",
        phone="tg:42",
        dialog_context={DIALOG_MODE_KEY: DIALOG_MODE_MANAGE},
    )
    fake_session = _FakeSession(org=org, customer=customer)
    monkeypatch.setattr(
        "bot.llm.tools.AsyncSessionLocal",
        lambda: _FakeSessionManager(fake_session),
    )
    scheduled = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    notified = {"text": ""}

    async def fake_cancel(_session, org_id, customer_id, reason):
        return (
            {
                "id": 7,
                "scheduled_at": scheduled.isoformat(),
                "cancel_reason": "Клиент отменил",
            },
            customer,
        )

    async def fake_notify(org_arg, cust_arg, scheduled_at, reason):
        notified["text"] = reason

    async def fake_send(org_arg, cust_arg, text):
        notified["send"] = text
        return True

    monkeypatch.setattr(
        "bot.llm.tools.appointment_service.cancel_appointment_by_customer",
        fake_cancel,
    )
    monkeypatch.setattr("bot.llm.tools._notify_client_cancel", fake_notify)
    monkeypatch.setattr(
        "bot.llm.tools.notification_service.send_customer_message",
        fake_send,
    )

    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    result = await tool_by_name(ctx, "cancel_appointment", mode=DIALOG_MODE_MANAGE)("")
    assert "отменена" in result
    assert notified["text"] == "Клиент отменил"


@pytest.mark.asyncio
async def test_edit_appointment_tool_reschedules(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(id=1, timezone="UTC")
    customer = SimpleNamespace(id=10, name="Ali", phone="wa:1", dialog_context={})
    appt = SimpleNamespace(id=7, customer_id=10)
    fake_session = _FakeSession(org=org, customer=customer)
    monkeypatch.setattr(
        "bot.llm.tools.AsyncSessionLocal",
        lambda: _FakeSessionManager(fake_session),
    )
    local_dt = datetime(2026, 6, 2, 15, 0, tzinfo=timezone.utc)

    async def fake_get_active(_session, org_id, customer_id):
        return appt, customer

    async def fake_reschedule(_session, org_id, appt_id, **kwargs):
        return ({"id": appt_id, "status": "new"}, customer, local_dt)

    sent = {"ok": False}

    async def fake_reschedule_notify(org_arg, cust_arg, local):
        sent["when"] = local

    monkeypatch.setattr(
        "bot.llm.tools.appointment_service.get_active_appointment_for_customer",
        fake_get_active,
    )
    monkeypatch.setattr(
        "bot.llm.tools.appointment_service.reschedule_appointment",
        fake_reschedule,
    )
    monkeypatch.setattr("bot.llm.tools._notify_client_reschedule", fake_reschedule_notify)

    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    result = await tool_by_name(ctx, "edit_appointment", mode=DIALOG_MODE_MANAGE)(
        "2026-06-02",
        "15:00",
    )
    assert "перенесена" in result
    assert sent["when"] == local_dt


@pytest.mark.asyncio
async def test_resolve_tool_mode_active(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=5, dialog_context={})

    async def fake_active(_session, org_id, customer_id):
        return (SimpleNamespace(id=1), customer)

    monkeypatch.setattr(
        "bot.llm.tools.appointment_service.get_active_appointment_for_customer",
        fake_active,
    )
    mode = await resolve_tool_mode(_FakeSession(), 1, customer)
    assert mode == DIALOG_MODE_MANAGE

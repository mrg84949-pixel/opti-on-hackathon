from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.db.models import AppointmentStatus
from bot.services import customer_service


def test_channel_from_phone_and_reminders_disabled():
    assert customer_service.channel_from_phone("tg:1") == "telegram"
    assert customer_service.channel_from_phone("wa:1") == "whatsapp"
    assert customer_service.channel_from_phone("web:1") == "web"
    assert customer_service.reminders_disabled(SimpleNamespace(disable_reminders=True)) is True
    assert customer_service.reminders_disabled(SimpleNamespace(disable_reminders=False)) is False


def test_sandbox_phone_prefix_constant():
    assert customer_service.SANDBOX_PHONE_PREFIX == "web:admin-sandbox-"


def test_truncate_helpers():
    assert customer_service._truncate(None) is None
    assert customer_service._truncate("  ") is None
    assert customer_service._truncate("short") == "short"
    long_text = "x" * 200
    assert customer_service._truncate(long_text, max_len=20).endswith("…")


@pytest.mark.asyncio
async def test_get_customer_for_org_wrong_org():
    customer = SimpleNamespace(id=1, org_id=2)
    session = SimpleNamespace(get=AsyncMock(return_value=customer))
    assert await customer_service.get_customer_for_org(session, 1, 1) is None


@pytest.mark.asyncio
async def test_set_mute_and_clear_mute(monkeypatch: pytest.MonkeyPatch):
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    customer = SimpleNamespace(
        id=5,
        org_id=1,
        muted_until=None,
        phone="tg:1",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=customer))
    monkeypatch.setattr(customer_service, "count_no_shows", AsyncMock(return_value=2))

    result = await customer_service.set_mute(session, 5, 1, days=3)
    assert result is not None
    assert result["customer_id"] == 5
    assert customer.muted_until is not None

    cleared = await customer_service.clear_mute(session, 5, 1)
    assert cleared is not None
    assert customer.muted_until is None


@pytest.mark.asyncio
async def test_set_reminders_disabled(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=3, org_id=1, disable_reminders=False)
    session = SimpleNamespace(get=AsyncMock(return_value=customer))
    result = await customer_service.set_reminders_disabled(session, 3, 1, disabled=True)
    assert result["disable_reminders"] is True


@pytest.mark.asyncio
async def test_maybe_auto_mute_applies_when_threshold(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=1, org_id=1, muted_until=None)
    session = SimpleNamespace()
    monkeypatch.setattr(customer_service, "count_no_shows", AsyncMock(return_value=3))
    applied = await customer_service.maybe_auto_mute(session, customer)
    assert applied is True
    assert customer.muted_until is not None


@pytest.mark.asyncio
async def test_maybe_auto_mute_skips_when_already_muted(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    customer = SimpleNamespace(id=1, muted_until=now + timedelta(days=1))
    session = SimpleNamespace()
    monkeypatch.setattr(customer_service, "count_no_shows", AsyncMock(return_value=99))
    assert await customer_service.maybe_auto_mute(session, customer) is False


@pytest.mark.asyncio
async def test_mute_for_human_handoff(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=1, org_id=1, muted_until=None)
    session = SimpleNamespace()
    monkeypatch.setattr(customer_service, "get_customer_for_org", AsyncMock(return_value=customer))
    monkeypatch.setattr(customer_service, "set_mute", AsyncMock(return_value={"customer_id": 1}))
    assert await customer_service.mute_for_human_handoff(session, 1, 1) is True


def test_serialize_appointment_row():
    appt = SimpleNamespace(
        id=9,
        scheduled_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        status=AppointmentStatus.CONFIRMED,
        cancel_reason=None,
        crm_appointment_id="crm-1",
    )
    row = customer_service._serialize_appointment_row(appt)
    assert row["status"] == "confirmed"
    assert row["crm_appointment_id"] == "crm-1"


def test_preview_text_truncates():
    long_text = "a" * 600
    assert customer_service._preview_text(long_text).endswith("…")
    assert len(customer_service._preview_text(long_text)) == 512


@pytest.mark.asyncio
async def test_send_admin_message_unsupported_channel():
    customer = SimpleNamespace(id=1, org_id=1, phone="web:1")
    session = SimpleNamespace(get=AsyncMock(return_value=customer))
    result = await customer_service.send_admin_message(session, 1, 1, text="hello")
    assert result["error_code"] == "unsupported_channel"


@pytest.mark.asyncio
async def test_send_admin_message_success(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=1, org_id=1, phone="tg:42")
    org = SimpleNamespace(id=1)
    session = SimpleNamespace(get=AsyncMock(side_effect=[customer, org]), add=lambda _x: None, flush=AsyncMock())
    from bot.services.outbound_result import OutboundSendResult

    monkeypatch.setattr(
        customer_service.notification_service,
        "send_customer_message",
        AsyncMock(return_value=OutboundSendResult.success("telegram")),
    )
    result = await customer_service.send_admin_message(session, 1, 1, text="Привет")
    assert result is not None
    assert result["notification_sent"] is True
    assert result["channel"] == "telegram"


class _ScalarsAll:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarsAll(self._rows)

    def scalar_one(self):
        return self._rows[0] if self._rows else 0


class _ProfileSession:
    def __init__(self, customer, logs, appointments):
        self.customer = customer
        self.logs = logs
        self.appointments = appointments
        self._get_calls = 0

    async def get(self, model, key):
        self._get_calls += 1
        if self._get_calls == 1:
            return self.customer
        return SimpleNamespace(id=key)

    async def execute(self, stmt):
        sql = str(stmt)
        if "bot_interaction_log" in sql.lower():
            return _ExecuteResult(self.logs)
        if "appointment" in sql.lower() and "count" in sql.lower():
            return _ExecuteResult([len(self.appointments)])
        return _ExecuteResult(self.appointments)


@pytest.mark.asyncio
async def test_erase_customer_for_org(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=7, org_id=1, phone="tg:1")
    session = SimpleNamespace(
        get=AsyncMock(return_value=customer),
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=3, scalar_one=lambda: 2)),
        delete=AsyncMock(),
        flush=AsyncMock(),
    )
    session.delete = AsyncMock()
    result = await customer_service.erase_customer_for_org(session, 7, 1)
    assert result["logs_removed"] == 3
    assert result["appointments_removed"] == 2
    session.delete.assert_awaited_once_with(customer)


@pytest.mark.asyncio
async def test_get_conversation_with_logs(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=2, org_id=1, phone="tg:42", muted_until=None)
    log = SimpleNamespace(
        id=1,
        user_message_preview="Q",
        reply_preview="A",
        status="ok",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=customer),
        execute=AsyncMock(return_value=_ExecuteResult([log])),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(customer_service, "count_no_shows", AsyncMock(return_value=1))
    monkeypatch.setattr(customer_service, "maybe_auto_mute", AsyncMock(return_value=False))
    convo = await customer_service.get_conversation(session, 2, 1)
    assert convo is not None
    assert convo["channel"] == "telegram"
    assert len(convo["messages"]) == 2


@pytest.mark.asyncio
async def test_get_customer_profile(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=5,
        org_id=1,
        name="Ali",
        phone="tg:99",
        muted_until=None,
        disable_reminders=False,
        dialog_context=None,
    )
    log = SimpleNamespace(
        user_message_preview="Hi",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        status="ok",
    )
    appt = SimpleNamespace(
        id=1,
        scheduled_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        status=AppointmentStatus.NEW,
        cancel_reason=None,
        crm_appointment_id=None,
    )
    session = _ProfileSession(customer, [log], [appt])
    monkeypatch.setattr(customer_service, "count_no_shows", AsyncMock(return_value=0))
    profile = await customer_service.get_customer_profile(session, 5, 1)
    assert profile is not None
    assert profile["customer"]["channel"] == "telegram"
    assert profile["recent_messages"][0]["text"] == "Hi"

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import types

import pytest

import bot.api.whatsapp as whatsapp
from bot.automation import reminders
from bot.services import notification_service
from bot.services.outbound_result import OutboundSendResult
from bot.llm.context import TurnContext
from bot.llm.tools import tool_by_name


@pytest.mark.asyncio
async def test_whatsapp_webhook_invokes_llm_and_send(monkeypatch):
    captured = {}

    fake_org = types.SimpleNamespace(
        id=1,
        whatsapp_provider="green",
        whatsapp_instance_id="1",
        whatsapp_api_token="token",
        whatsapp_meta_phone_number_id=None,
        whatsapp_meta_access_token=None,
        whatsapp_meta_reminder_template_name=None,
        whatsapp_meta_reminder_template_lang="ru",
        whatsapp_broadcast_quota_monthly=500,
        whatsapp_broadcast_sent_count=0,
        whatsapp_broadcast_month_key=None,
        whatsapp_broadcast_locked=False,
    )

    async def fake_resolve_org_green(_instance_hint):
        return fake_org

    async def fake_get_ai_response(user_id: str, user_text: str, db_memory: dict, channel: str, org_id=None):
        captured["user_id"] = user_id
        captured["user_text"] = user_text
        captured["channel"] = channel
        return "ok-reply"

    async def fake_send(org, chat_id: str, text: str):
        captured["sent_chat_id"] = chat_id
        captured["sent_text"] = text

    async def always_claim(*_args, **_kwargs):
        return True

    monkeypatch.setattr(whatsapp, "get_ai_response", fake_get_ai_response)
    monkeypatch.setattr(whatsapp, "_resolve_org_green", fake_resolve_org_green)
    monkeypatch.setattr(whatsapp, "claim_inbound_event_or_duplicate", always_claim)
    monkeypatch.setattr(whatsapp, "send_whatsapp_text", fake_send)

    class FakeRequest:
        async def json(self):
            return {
                "idMessage": "green-critical-1",
                "senderData": {"chatId": "777@c.us"},
                "messageData": {"textMessageData": {"textMessage": "Запиши меня"}},
            }

    result = await whatsapp.whatsapp_green_webhook(FakeRequest())

    assert result["status"] == "ok"
    assert captured["channel"] == "whatsapp"
    assert captured["user_text"] == "Запиши меня"
    assert captured["sent_chat_id"] == "777@c.us"
    assert captured["sent_text"] == "ok-reply"


@pytest.mark.asyncio
async def test_transfer_to_human_sends_admin_notification(monkeypatch):
    called = {"notified": 0, "muted": 0}

    async def fake_mute(session, customer_id, org_id):
        called["muted"] += 1
        assert customer_id == 42
        assert org_id == 1
        return True

    async def fake_notify(*, org_id: int, customer_id: int):
        called["notified"] += 1
        assert org_id == 1
        assert customer_id == 42
        return 1

    class _Session:
        committed = False

        async def commit(self):
            self.committed = True

    class _SessionManager:
        session = _Session()

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("bot.services.handoff_service.customer_service.mute_for_human_handoff", fake_mute)
    monkeypatch.setattr("bot.services.handoff_service.AsyncSessionLocal", lambda: _SessionManager())
    monkeypatch.setattr("bot.services.handoff_service.notify_admins_about_human_transfer", fake_notify)
    ctx = TurnContext(org_id=1, customer_id=42, services_catalog="")
    transfer_to_human = tool_by_name(ctx, "transfer_to_human")
    result = await transfer_to_human()

    assert "администратору" in result
    assert ctx.human_transfer_requested is True
    assert called["notified"] == 1
    assert called["muted"] == 1


@pytest.mark.asyncio
async def test_process_24h_reminders_marks_sent(monkeypatch):
    now = datetime.now(timezone.utc)
    org = types.SimpleNamespace(
        id=1,
        whatsapp_instance_id="1",
        whatsapp_api_token="token",
        bot_enabled=True,
        billing_paid_until=None,
    )
    customer = types.SimpleNamespace(phone="wa:777@c.us", organization=org)
    appointment = types.SimpleNamespace(
        customer=customer,
        scheduled_at=now + timedelta(hours=24, minutes=5),
        status=None,
        reminder_24h_sent_at=None,
    )

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [appointment]

    class FakeSession:
        committed = False

        async def execute(self, _):
            return FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = FakeSession()

    class FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_send_to_customer(org_arg, customer_arg, text_arg):
        assert org_arg is org
        assert customer_arg is customer
        assert "Напоминание" in text_arg
        return OutboundSendResult.success()

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send_to_customer)

    await reminders.process_24h_reminders()

    assert fake_session.committed is True
    assert appointment.reminder_24h_sent_at is not None


@pytest.mark.asyncio
async def test_process_2h_reminders_marks_sent(monkeypatch):
    now = datetime.now(timezone.utc)
    org = types.SimpleNamespace(
        id=1,
        whatsapp_instance_id="1",
        whatsapp_api_token="token",
        bot_enabled=True,
        billing_paid_until=None,
    )
    customer = types.SimpleNamespace(id=2, phone="tg:12345", organization=org, disable_reminders=False, muted_until=None)
    appointment = types.SimpleNamespace(
        customer=customer,
        scheduled_at=now + timedelta(hours=2, minutes=5),
        status=None,
        reminder_2h_sent_at=None,
    )

    class FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [appointment]

    class FakeSession:
        committed = False

        async def execute(self, _):
            return FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = FakeSession()

    class FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_send(org_arg, customer_arg, text_arg):
        assert "2 часа" in text_arg
        return OutboundSendResult.success(channel="telegram")

    monkeypatch.setattr(reminders, "AsyncSessionLocal", lambda: FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)

    await reminders.process_2h_reminders()

    assert fake_session.committed is True
    assert appointment.reminder_2h_sent_at is not None

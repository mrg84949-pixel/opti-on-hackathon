"""Wave 2 B.4 T05: booking with AI_BOOKING_FSM=0 — LLM/tools path, no booking_fsm in logs."""
from __future__ import annotations

import pytest

import bot.llm.llm_engine as llm_engine
from bot.db.models import Appointment
from bot.llm.booking_fsm import BOOKING_STEP_KEY, try_handle_booking_fsm
from bot.llm.booking_draft import PENDING_BOOKING_KEY
from bot.llm.scenarios import BOOKING_HAPPY_PATH
from bot.llm.providers.scripted_scenario import ScriptedScenarioProvider
from bot.llm.tools import DIALOG_MODE_KEY, DIALOG_MODE_MANAGE, LAST_APPOINTMENT_ID_KEY

from test_bot_scenarios import (
    _FakeSession,
    _FakeSessionManager,
    _customer,
    _org,
    _patch_crm_provider,
)

LEAK_MARKERS = (
    "Сообщите клиенту",
    "cancel_appointment",
    "edit_appointment",
    "[Инструкция",
    "статус «новая»",
    "Контекст сжат",
)


def _patch_fsm_off_scenario_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    org,
    customer,
    fake_session: _FakeSession,
    log_statuses: list[str],
    services_catalog: str = "- Консультация: 5 000 ₽",
) -> None:
    """Like test_bot_scenarios patch but keeps real FSM/shortcut handlers; FSM flag off."""
    monkeypatch.setenv("AI_BOOKING_FSM", "0")
    llm_engine._sessions.clear()
    fake_session.org = org
    fake_session.customer = customer

    provider = ScriptedScenarioProvider(BOOKING_HAPPY_PATH)
    monkeypatch.setattr(
        llm_engine,
        "get_llm_provider",
        lambda _org=None: provider,
    )
    session_factory = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr(llm_engine, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", session_factory)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", session_factory)
    monkeypatch.setattr("bot.llm.booking_fsm.AsyncSessionLocal", session_factory)
    monkeypatch.setattr("bot.services.booking_deterministic.AsyncSessionLocal", session_factory)
    # "да" on confirm turn hits client_change_intent before LLM; must not use live DB.
    monkeypatch.setattr("bot.services.client_change_intent.AsyncSessionLocal", session_factory)

    async def fake_get_or_create(_session, *, org_id, channel_phone, name=None):
        _ = org_id, channel_phone, name
        return customer

    async def fake_maybe_auto_mute(_session, _customer):
        return False

    async def fake_catalog(_session, _org_id):
        return services_catalog

    async def spy_persist(**kwargs):
        log_statuses.append(kwargs.get("status", ""))

    async def fake_notify_new(**_kwargs):
        return 1

    async def fake_price_minor(_session, _org_id, _service):
        return 500_000

    async def fake_notify_cancel(*_args, **_kwargs):
        return None

    async def fake_notify_reschedule(*_args, **_kwargs):
        return None

    async def fake_send_message(*_args, **_kwargs):
        return True

    monkeypatch.setattr(llm_engine, "get_or_create_customer_for_channel", fake_get_or_create)
    monkeypatch.setattr(llm_engine.customer_service, "maybe_auto_mute", fake_maybe_auto_mute)
    monkeypatch.setattr(llm_engine, "load_services_catalog_for_org", fake_catalog)
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", spy_persist)
    monkeypatch.setattr("bot.llm.tools.notify_admins_about_new_appointment", fake_notify_new)
    monkeypatch.setattr("bot.llm.tools.resolve_service_price_minor", fake_price_minor)
    monkeypatch.setattr("bot.llm.tools._notify_client_cancel", fake_notify_cancel)
    monkeypatch.setattr("bot.llm.tools._notify_client_reschedule", fake_notify_reschedule)
    monkeypatch.setattr(
        "bot.llm.tools.notification_service.send_customer_message",
        fake_send_message,
    )
    _patch_crm_provider(monkeypatch)


@pytest.mark.asyncio
async def test_t05_fsm_off_scripted_booking_no_fsm_in_log(monkeypatch: pytest.MonkeyPatch):
    customer = _customer()
    fake_session = _FakeSession(org=_org(), customer=customer)
    log_statuses: list[str] = []
    _patch_fsm_off_scenario_env(
        monkeypatch,
        org=fake_session.org,
        customer=customer,
        fake_session=fake_session,
        log_statuses=log_statuses,
    )

    replies: list[str] = []
    for turn in BOOKING_HAPPY_PATH.turns:
        reply = await llm_engine.get_ai_response(
            "user-1",
            turn.user_text,
            channel="web",
            org_id=fake_session.org.id,
        )
        replies.append(reply)

    assert "booking_fsm" not in log_statuses
    assert log_statuses, "expected interaction log entries"
    assert len(replies) == 2
    assert "Проверьте запись" in replies[0]
    assert "оформлена" in replies[1].lower()
    for marker in LEAK_MARKERS:
        assert marker not in replies[1]
    assert customer.dialog_context.get(DIALOG_MODE_KEY) == DIALOG_MODE_MANAGE
    assert PENDING_BOOKING_KEY not in customer.dialog_context
    assert customer.dialog_context.get(BOOKING_STEP_KEY) is None
    assert customer.dialog_context.get(LAST_APPOINTMENT_ID_KEY) == 99
    appts = [obj for obj in fake_session.added if isinstance(obj, Appointment)]
    assert len(appts) == 1
    assert appts[0].service_name == "Консультация"


@pytest.mark.asyncio
async def test_fsm_entry_returns_none_when_flag_off(monkeypatch: pytest.MonkeyPatch):
    """Thin regression: FSM guard returns None when AI_BOOKING_FSM=0."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    customer = SimpleNamespace(id=2, phone="tg:9", dialog_context={})
    monkeypatch.setenv("AI_BOOKING_FSM", "0")

    class _Mgr:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("bot.llm.booking_fsm.AsyncSessionLocal", lambda: _Mgr())
    monkeypatch.setattr(
        "bot.llm.booking_fsm.get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(
        "bot.llm.booking_fsm.load_services_catalog_for_org",
        AsyncMock(return_value="- Консультация: 5 000 ₽"),
    )

    assert (
        await try_handle_booking_fsm(
            org_id=1, channel="telegram", user_id="9", user_text="хочу записаться"
        )
        is None
    )

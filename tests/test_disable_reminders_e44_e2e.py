"""Wave 2 E.2: disable_reminders gate (T44) — tool path, sanitize, job skip."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.llm.client_messages import sanitize_client_reply
from bot.llm.scenarios import BotScenario, ScenarioAction, ScenarioTurn
from bot.services import customer_service
from test_bot_scenarios import _customer, _run_scenario

DISABLE_REMINDERS_SCENARIO = BotScenario(
    name="disable_reminders",
    turns=[
        ScenarioTurn(
            user_text="Не присылайте напоминания",
            actions=[ScenarioAction(tool="disable_reminders", kwargs={"disabled": True})],
        ),
    ],
)

_TOOL_ASSISTANT_REPLY = (
    "Напоминания о записи отключены. Сообщите клиенту, что мы больше не будем присылать "
    "напоминания за 24 и 2 часа до записи. Подтверждения, отмены и другие важные сообщения "
    "по-прежнему могут приходить."
)


@pytest.mark.asyncio
async def test_e44_scripted_disable_reminders_sets_flag(monkeypatch: pytest.MonkeyPatch):
    customer = _customer()
    replies, fake_session = await _run_scenario(
        monkeypatch,
        DISABLE_REMINDERS_SCENARIO,
        customer=customer,
    )

    assert len(replies) == 1
    assert customer.disable_reminders is True
    assert "сообщите клиенту" not in replies[0].lower()


def test_e44_client_reply_sanitized_no_leak():
    sanitized = sanitize_client_reply(_TOOL_ASSISTANT_REPLY)
    lowered = sanitized.lower()
    assert "сообщите клиенту" not in lowered
    assert "Сообщите клиенту" not in sanitized


def test_e44_reminder_jobs_skip_opted_out():
    assert customer_service.reminders_disabled(SimpleNamespace(disable_reminders=True)) is True
    assert customer_service.reminders_disabled(SimpleNamespace(disable_reminders=False)) is False
    assert customer_service.reminders_disabled(SimpleNamespace()) is False

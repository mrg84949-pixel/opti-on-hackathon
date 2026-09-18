"""Wave 2 E.1: Mute gate (T43) — MUTE_USER_MESSAGE, no booking/LLM."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.llm.llm_engine as llm_engine
from bot.services import customer_service
from test_llm_engine_extra import (
    _FakeSession,
    _FakeSessionManager,
    _org,
)

ROOT = Path(__file__).resolve().parents[1]


def _load_telegram_poll_module():
    path = ROOT / "scripts" / "telegram_poll.py"
    spec = importlib.util.spec_from_file_location("telegram_poll", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["telegram_poll"] = module
    spec.loader.exec_module(module)
    return module


def _muted_customer():
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=77,
        muted_until=now + timedelta(days=7),
        dialog_context={},
    )


def _patch_muted_customer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_customer(*args, **kwargs):
        return _muted_customer()

    async def fake_maybe_auto_mute(session, customer):
        return False

    monkeypatch.setattr(llm_engine, "get_or_create_customer_for_channel", fake_customer)
    monkeypatch.setattr(llm_engine.customer_service, "maybe_auto_mute", fake_maybe_auto_mute)
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): _org()})),
    )


@pytest.mark.asyncio
async def test_e43_mute_blocks_booking_intent(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    _patch_muted_customer(monkeypatch)
    captured: dict[str, object] = {}

    async def fake_persist(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)

    result = await llm_engine.get_ai_response(
        "1946206956",
        "хочу записаться",
        {},
        channel="telegram",
        org_id=1,
    )

    assert result == customer_service.MUTE_USER_MESSAGE
    assert captured["status"] == "muted"
    assert captured["reply"] == customer_service.MUTE_USER_MESSAGE


@pytest.mark.asyncio
async def test_e43_mute_skips_booking_and_llm(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    _patch_muted_customer(monkeypatch)

    booking_shortcut = AsyncMock(return_value=None)
    booking_fsm = AsyncMock(return_value=None)
    llm_session_calls: list[bool] = []

    class _NoLlmProvider:
        provider_name = "stub"

        async def create_session(self, _setup):
            llm_session_calls.append(True)
            raise AssertionError("LLM session must not be created for muted customer")

        async def send_turn(self, _session, _user_text):
            llm_session_calls.append(True)
            raise AssertionError("LLM send_turn must not run for muted customer")

    def fake_get_llm_provider(_org=None):
        return _NoLlmProvider()

    monkeypatch.setattr(
        llm_engine.booking_deterministic,
        "try_handle_booking_shortcut",
        booking_shortcut,
    )
    monkeypatch.setattr(llm_engine.booking_fsm, "try_handle_booking_fsm", booking_fsm)
    monkeypatch.setattr(llm_engine, "get_llm_provider", fake_get_llm_provider)
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", AsyncMock())

    result = await llm_engine.get_ai_response(
        "1946206956",
        "хочу записаться",
        {},
        channel="telegram",
        org_id=1,
    )

    assert result == customer_service.MUTE_USER_MESSAGE
    booking_shortcut.assert_not_called()
    booking_fsm.assert_not_called()
    assert llm_session_calls == []


@pytest.mark.asyncio
async def test_e43_poll_non_start_muted(monkeypatch: pytest.MonkeyPatch):
    telegram_poll = _load_telegram_poll_module()
    llm_engine._sessions.clear()
    operational_org = _org()
    sent: list[str] = []

    async def fake_resolve(_token):
        return operational_org

    async def fake_send(_org, _chat_id, text):
        sent.append(text)
        return True

    _patch_muted_customer(monkeypatch)
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", AsyncMock())
    monkeypatch.setattr(telegram_poll, "resolve_org_by_telegram_bot_token", fake_resolve)
    monkeypatch.setattr(telegram_poll, "ingress_telegram_bot_token", lambda: "token")
    monkeypatch.setattr(telegram_poll, "send_telegram_message", fake_send)

    await telegram_poll._handle_message(123, "запишите меня")

    assert sent == [customer_service.MUTE_USER_MESSAGE]

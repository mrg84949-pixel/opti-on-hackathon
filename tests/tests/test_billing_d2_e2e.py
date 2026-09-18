"""Wave 2 D.2: Billing gate expired — TARIFF_BLOCKED_MESSAGE, no booking/LLM."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import bot.llm.llm_engine as llm_engine
from bot.billing_access import TARIFF_BLOCKED_MESSAGE
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


def _blocked_org(org_id: int = 1):
    return _org(
        org_id,
        billing_paid_until=datetime.now(timezone.utc) - timedelta(days=1),
    )


@pytest.mark.asyncio
async def test_d2_billing_blocks_booking_intent(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    blocked = _blocked_org()
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): blocked})),
    )
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

    assert result == TARIFF_BLOCKED_MESSAGE
    assert captured["status"] == "billing_blocked"
    assert captured["reply"] == TARIFF_BLOCKED_MESSAGE


@pytest.mark.asyncio
async def test_d2_billing_skips_booking_paths(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    blocked = _blocked_org()
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): blocked})),
    )

    booking_shortcut = AsyncMock(return_value=None)
    booking_fsm = AsyncMock(return_value=None)
    provider_called: list[bool] = []

    def fake_get_llm_provider(_org=None):
        provider_called.append(True)
        raise AssertionError("get_llm_provider must not be called when billing expired")

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

    assert result == TARIFF_BLOCKED_MESSAGE
    booking_shortcut.assert_not_called()
    booking_fsm.assert_not_called()
    assert provider_called == []


@pytest.mark.asyncio
async def test_d2_poll_non_start_blocked(monkeypatch: pytest.MonkeyPatch):
    telegram_poll = _load_telegram_poll_module()
    llm_engine._sessions.clear()
    expired_org = _blocked_org()
    sent: list[str] = []

    async def fake_resolve(_token):
        return expired_org

    async def fake_send(_org, _chat_id, text):
        sent.append(text)
        return True

    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(
            _FakeSession(get_results={(llm_engine.Organization, 1): expired_org})
        ),
    )
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", AsyncMock())
    monkeypatch.setattr(telegram_poll, "resolve_org_by_telegram_bot_token", fake_resolve)
    monkeypatch.setattr(telegram_poll, "ingress_telegram_bot_token", lambda: "token")
    monkeypatch.setattr(telegram_poll, "send_telegram_message", fake_send)

    await telegram_poll._handle_message(123, "запишите меня")

    assert sent == [TARIFF_BLOCKED_MESSAGE]

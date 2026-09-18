from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.billing_access import BOT_PAUSED_MESSAGE, TARIFF_BLOCKED_MESSAGE

ROOT = Path(__file__).resolve().parents[1]


def _load_telegram_poll_module():
    path = ROOT / "scripts" / "telegram_poll.py"
    spec = importlib.util.spec_from_file_location("telegram_poll", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["telegram_poll"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_poll_start_sends_paused_message_when_bot_disabled(monkeypatch: pytest.MonkeyPatch):
    telegram_poll = _load_telegram_poll_module()
    org = SimpleNamespace(
        id=1,
        bot_enabled=False,
        billing_paid_until=None,
        telegram_bot_token="token",
    )
    sent: list[str] = []

    async def fake_resolve(_token):
        return org

    async def fake_send(_org, _chat_id, text):
        sent.append(text)
        return True

    monkeypatch.setattr(telegram_poll, "resolve_org_by_telegram_bot_token", fake_resolve)
    monkeypatch.setattr(telegram_poll, "ingress_telegram_bot_token", lambda: "token")
    monkeypatch.setattr(telegram_poll, "send_telegram_message", fake_send)

    await telegram_poll._handle_message(123, "/start")

    assert sent == [BOT_PAUSED_MESSAGE]


@pytest.mark.asyncio
async def test_poll_start_sends_tariff_message_when_billing_expired(monkeypatch: pytest.MonkeyPatch):
    telegram_poll = _load_telegram_poll_module()
    org = SimpleNamespace(
        id=1,
        bot_enabled=True,
        billing_paid_until=datetime.now(timezone.utc) - timedelta(hours=1),
        telegram_bot_token="token",
    )
    sent: list[str] = []

    async def fake_resolve(_token):
        return org

    async def fake_send(_org, _chat_id, text):
        sent.append(text)
        return True

    monkeypatch.setattr(telegram_poll, "resolve_org_by_telegram_bot_token", fake_resolve)
    monkeypatch.setattr(telegram_poll, "ingress_telegram_bot_token", lambda: "token")
    monkeypatch.setattr(telegram_poll, "send_telegram_message", fake_send)

    await telegram_poll._handle_message(123, "/start")

    assert sent == [TARIFF_BLOCKED_MESSAGE]


@pytest.mark.asyncio
async def test_poll_start_sends_greeting_when_operational(monkeypatch: pytest.MonkeyPatch):
    telegram_poll = _load_telegram_poll_module()
    org = SimpleNamespace(
        id=1,
        bot_enabled=True,
        billing_paid_until=None,
        telegram_bot_token="token",
    )
    sent: list[str] = []

    async def fake_resolve(_token):
        return org

    async def fake_send(_org, _chat_id, text):
        sent.append(text)
        return True

    monkeypatch.setattr(telegram_poll, "resolve_org_by_telegram_bot_token", fake_resolve)
    monkeypatch.setattr(telegram_poll, "ingress_telegram_bot_token", lambda: "token")
    monkeypatch.setattr(telegram_poll, "send_telegram_message", fake_send)

    await telegram_poll._handle_message(123, "/start")

    assert sent == [telegram_poll.START_REPLY]


@pytest.mark.asyncio
async def test_poll_start_sends_custom_welcome(monkeypatch: pytest.MonkeyPatch):
    telegram_poll = _load_telegram_poll_module()
    org = SimpleNamespace(
        id=1,
        bot_enabled=True,
        billing_paid_until=None,
        bot_welcome_message="Кастомное приветствие",
        telegram_bot_token="token",
    )
    sent: list[str] = []

    async def fake_resolve(_token):
        return org

    async def fake_send(_org, _chat_id, text):
        sent.append(text)
        return True

    monkeypatch.setattr(telegram_poll, "resolve_org_by_telegram_bot_token", fake_resolve)
    monkeypatch.setattr(telegram_poll, "ingress_telegram_bot_token", lambda: "token")
    monkeypatch.setattr(telegram_poll, "send_telegram_message", fake_send)

    await telegram_poll._handle_message(123, "/start")

    assert sent == ["Кастомное приветствие"]

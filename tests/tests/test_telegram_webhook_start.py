from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

import bot.api.telegram as telegram_api
from bot.billing_access import BOT_PAUSED_MESSAGE


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(telegram_api.router, prefix="/bot")
    return app


async def _always_claim(*_args, **_kwargs):
    return True


@pytest.mark.asyncio
async def test_telegram_webhook_start_sends_welcome_without_llm(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(
        id=1,
        bot_enabled=True,
        billing_paid_until=None,
        bot_welcome_message="Добро пожаловать в клинику!",
        telegram_bot_token="token",
    )

    async def fake_resolve(_token: str):
        return org

    ai_called = False
    captured: dict[str, str] = {}

    async def fake_ai(**_kwargs):
        nonlocal ai_called
        ai_called = True
        return "llm"

    async def fake_send(_org, _chat_id, text):
        captured["text"] = text
        return True

    monkeypatch.setattr(telegram_api, "resolve_org_for_telegram_webhook", fake_resolve)
    monkeypatch.setattr(telegram_api, "claim_inbound_event_or_duplicate", _always_claim)
    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai)
    monkeypatch.setattr(telegram_api, "send_telegram_message", fake_send)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/bot/webhook",
            json={"update_id": 5001, "message": {"chat": {"id": 777}, "text": "/start"}},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert ai_called is False
    assert captured["text"] == "Добро пожаловать в клинику!"


@pytest.mark.asyncio
async def test_telegram_webhook_start_frozen_org(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(
        id=1,
        bot_enabled=False,
        billing_paid_until=None,
        bot_welcome_message="ignored",
        telegram_bot_token="token",
    )

    async def fake_resolve(_token: str):
        return org

    ai_called = False
    captured: dict[str, str] = {}

    async def fake_ai(**_kwargs):
        nonlocal ai_called
        ai_called = True
        return "llm"

    async def fake_send(_org, _chat_id, text):
        captured["text"] = text
        return True

    monkeypatch.setattr(telegram_api, "resolve_org_for_telegram_webhook", fake_resolve)
    monkeypatch.setattr(telegram_api, "claim_inbound_event_or_duplicate", _always_claim)
    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai)
    monkeypatch.setattr(telegram_api, "send_telegram_message", fake_send)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/bot/webhook",
            json={"update_id": 5002, "message": {"chat": {"id": 777}, "text": "/start"}},
        )

    assert response.status_code == 200
    assert ai_called is False
    assert captured["text"] == BOT_PAUSED_MESSAGE

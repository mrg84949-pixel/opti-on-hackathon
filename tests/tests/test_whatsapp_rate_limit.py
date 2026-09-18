"""Per-sender (not per-IP) LLM rate limit for WhatsApp inbound — protects
spend from one number flooding the bot. Separate from the generic per-IP
webhook limit in bot/services/rate_limit.py."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import bot.api.whatsapp as whatsapp_api
from bot.config import settings
from bot.services import rate_limit as rate_limit_module


@pytest.fixture(autouse=True)
def _reset_limiter():
    rate_limit_module.reset_for_tests()
    yield
    rate_limit_module.reset_for_tests()


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(whatsapp_api.router, prefix="/bot")
    return app


async def _always_claim(*_args, **_kwargs):
    return True


def _org(**overrides):
    base = {
        "id": 1,
        "whatsapp_provider": None,
        "whatsapp_instance_id": None,
        "whatsapp_api_token": None,
        "whatsapp_meta_phone_number_id": None,
        "whatsapp_meta_access_token": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_wa_sender_rate_limited_allows_within_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_limit", 3)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_window_seconds", 60)

    for _ in range(3):
        assert await whatsapp_api._wa_sender_rate_limited(1, "777@c.us") is False


@pytest.mark.asyncio
async def test_wa_sender_rate_limited_blocks_over_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_limit", 3)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_window_seconds", 60)

    for _ in range(3):
        assert await whatsapp_api._wa_sender_rate_limited(1, "777@c.us") is False
    assert await whatsapp_api._wa_sender_rate_limited(1, "777@c.us") is True


@pytest.mark.asyncio
async def test_wa_sender_rate_limit_disabled_globally(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_limit", 1)

    for _ in range(5):
        assert await whatsapp_api._wa_sender_rate_limited(1, "777@c.us") is False


@pytest.mark.asyncio
async def test_wa_sender_rate_limit_disabled_when_limit_zero(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_limit", 0)

    for _ in range(5):
        assert await whatsapp_api._wa_sender_rate_limited(1, "777@c.us") is False


@pytest.mark.asyncio
async def test_wa_sender_rate_limit_is_per_org(monkeypatch: pytest.MonkeyPatch):
    """Same phone number, two different orgs — must not share a counter
    (cross-tenant bleed would be a T1 isolation violation)."""
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_limit", 1)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_window_seconds", 60)

    assert await whatsapp_api._wa_sender_rate_limited(1, "777@c.us") is False
    assert await whatsapp_api._wa_sender_rate_limited(1, "777@c.us") is True
    # org 2, same sender key — fresh counter
    assert await whatsapp_api._wa_sender_rate_limited(2, "777@c.us") is False


@pytest.mark.asyncio
async def test_green_webhook_skips_llm_when_sender_rate_limited(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_limit", 1)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_window_seconds", 60)

    fake_org = _org(id=1, whatsapp_provider="green")
    llm_calls = 0
    send_calls = 0

    async def fake_resolve_org(_instance_hint):
        return fake_org

    async def fake_llm(**_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "reply"

    async def fake_send(*_args, **_kwargs):
        nonlocal send_calls
        send_calls += 1
        return True

    monkeypatch.setattr(whatsapp_api, "_resolve_org_green", fake_resolve_org)
    monkeypatch.setattr(whatsapp_api, "claim_inbound_event_or_duplicate", _always_claim)
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)
    monkeypatch.setattr(whatsapp_api, "send_whatsapp_text", fake_send)

    app = _test_app()
    body = {
        "senderData": {"chatId": "777@c.us"},
        "messageData": {"textMessageData": {"textMessage": "hi"}},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/bot/whatsapp/webhook", json=body)
        second = await client.post("/bot/whatsapp/webhook", json=body)

    assert first.json()["status"] == "ok"
    assert second.json()["status"] == "rate_limited"
    assert llm_calls == 1
    assert send_calls == 1


@pytest.mark.asyncio
async def test_meta_webhook_skips_llm_when_sender_rate_limited(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "whatsapp_verify_token", "")
    monkeypatch.setattr(settings, "whatsapp_app_secret", "")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_limit", 1)
    monkeypatch.setattr(settings, "rate_limit_whatsapp_sender_window_seconds", 60)

    fake_org = _org(id=1, whatsapp_provider="meta")
    llm_calls = 0
    send_calls = 0

    async def fake_resolve_org(_phone_number_id):
        return fake_org

    async def fake_llm(**_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "reply"

    async def fake_send(*_args, **_kwargs):
        nonlocal send_calls
        send_calls += 1
        return True

    monkeypatch.setattr(whatsapp_api, "_resolve_org_meta", fake_resolve_org)
    monkeypatch.setattr(whatsapp_api, "claim_inbound_event_or_duplicate", _always_claim)
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)
    monkeypatch.setattr(whatsapp_api, "send_whatsapp_text", fake_send)

    app = _test_app()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "pnid-1"},
                            "messages": [
                                {"type": "text", "id": "wamid.1", "from": "77771112233", "text": {"body": "hi"}}
                            ],
                        }
                    }
                ]
            }
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/bot/whatsapp/meta", content=json.dumps(payload).encode("utf-8"))
        second = await client.post("/bot/whatsapp/meta", content=json.dumps(payload).encode("utf-8"))

    assert first.json()["status"] == "ok"
    assert second.json()["status"] == "rate_limited"
    assert llm_calls == 1
    assert send_calls == 1

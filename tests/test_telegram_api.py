from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

import bot.api.telegram as telegram_api


def _fake_org(org_id: int = 1):
    return SimpleNamespace(id=org_id, telegram_bot_token="test-bot-token")


def _mock_org_resolve(monkeypatch: pytest.MonkeyPatch, org=None):
    resolved = _fake_org() if org is None else org

    async def fake_resolve(_token: str):
        return resolved

    monkeypatch.setattr(telegram_api, "resolve_org_for_telegram_webhook", fake_resolve)
    return resolved


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(telegram_api.router, prefix="/bot")
    return app


class _FakeResponse:
    def __init__(self, *, status_code: int = 200):
        self.status_code = status_code

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return self.response


@pytest.mark.asyncio
async def test_send_telegram_message_success_and_failure(monkeypatch: pytest.MonkeyPatch):
    org = _fake_org()
    ok_client = _FakeClient(_FakeResponse(status_code=200))
    monkeypatch.setattr(
        "bot.services.telegram_org_service.httpx.AsyncClient",
        lambda timeout=20: ok_client,
    )
    result = await telegram_api.send_telegram_message(org, 777, "hello")
    assert result is True
    assert "bottest-bot-token/sendMessage" in ok_client.calls[0]["url"]
    assert ok_client.calls[0]["json"]["chat_id"] == 777

    fail_client = _FakeClient(_FakeResponse(status_code=500))
    monkeypatch.setattr(
        "bot.services.telegram_org_service.httpx.AsyncClient",
        lambda timeout=20: fail_client,
    )
    fail_result = await telegram_api.send_telegram_message(org, 777, "hello")
    assert fail_result is False

    empty_org = _fake_org()
    empty_org.telegram_bot_token = ""
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.telegram_token", "env-token")
    strict_result = await telegram_api.send_telegram_message(empty_org, 777, "hello")
    assert strict_result is False


async def _always_claim(*_args, **_kwargs):
    return True


@pytest.mark.asyncio
async def test_telegram_webhook_text_non_text_and_failure_paths(monkeypatch: pytest.MonkeyPatch):
    _mock_org_resolve(monkeypatch)
    monkeypatch.setattr(telegram_api, "claim_inbound_event_or_duplicate", _always_claim)
    captured = {}

    async def fake_ai(*, user_id, user_text, db_memory, channel, org_id=None):
        captured["user_id"] = user_id
        captured["user_text"] = user_text
        captured["channel"] = channel
        captured["org_id"] = org_id
        return "reply"

    async def fake_send(org, chat_id, text):
        captured["org_id_send"] = org.id
        captured["chat_id"] = chat_id
        captured["text"] = text
        return True

    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai)
    monkeypatch.setattr(telegram_api, "send_telegram_message", fake_send)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        text_resp = await client.post(
            "/bot/webhook",
            json={"update_id": 1001, "message": {"chat": {"id": 777}, "text": "hello"}},
        )
        non_text_resp = await client.post(
            "/bot/webhook",
            json={"update_id": 1002, "message": {"chat": {"id": 777}, "photo": [{"id": 1}]}},
        )

    assert text_resp.status_code == 200
    assert text_resp.json()["status"] == "ok"
    assert captured["user_id"] == "777"
    assert captured["channel"] == "telegram"
    assert captured["org_id"] == 1
    assert captured["chat_id"] == 777
    assert captured["text"] == "reply"
    assert non_text_resp.status_code == 200
    assert non_text_resp.json()["status"] == "ok"

    async def fake_ai_error(**_kwargs):
        raise RuntimeError("boom")

    async def fake_send_fail(_org, _chat_id, _text):
        return False

    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai_error)
    monkeypatch.setattr(telegram_api, "send_telegram_message", fake_send_fail)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        error_resp = await client.post(
            "/bot/webhook",
            json={"update_id": 1003, "message": {"chat": {"id": 777}, "text": "hello"}},
        )

    assert error_resp.status_code == 200
    assert error_resp.json()["status"] == "ok"


_WEBHOOK_PAYLOAD = {"update_id": 2001, "message": {"chat": {"id": 777}, "text": "hello"}}


@pytest.mark.asyncio
async def test_webhook_rejects_missing_secret_when_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "tg-secret-xyz")
    monkeypatch.setattr(telegram_api.settings, "telegram_webhook_secret", "tg-secret-xyz")
    ai_called = False

    async def fake_ai(**_kwargs):
        nonlocal ai_called
        ai_called = True
        return "reply"

    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post("/bot/webhook", json=_WEBHOOK_PAYLOAD)

    assert response.status_code == 403
    assert ai_called is False


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "tg-secret-xyz")
    monkeypatch.setattr(telegram_api.settings, "telegram_webhook_secret", "tg-secret-xyz")

    async def fake_resolve(_token: str):
        return None

    monkeypatch.setattr(telegram_api, "resolve_org_for_telegram_webhook", fake_resolve)
    ai_called = False

    async def fake_ai(**_kwargs):
        nonlocal ai_called
        ai_called = True
        return "reply"

    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/bot/webhook",
            json=_WEBHOOK_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        )

    assert response.status_code == 403
    assert ai_called is False


@pytest.mark.asyncio
async def test_webhook_accepts_valid_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "tg-secret-xyz")
    _mock_org_resolve(monkeypatch)
    captured = {}

    async def fake_ai(*, user_id, user_text, db_memory, channel, org_id=None):
        captured["user_id"] = user_id
        captured["channel"] = channel
        captured["org_id"] = org_id
        return "reply"

    async def fake_send(org, chat_id, text):
        captured["chat_id"] = chat_id
        captured["text"] = text
        captured["org_id_send"] = org.id
        return True

    monkeypatch.setattr(telegram_api, "claim_inbound_event_or_duplicate", _always_claim)
    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai)
    monkeypatch.setattr(telegram_api, "send_telegram_message", fake_send)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/bot/webhook",
            json=_WEBHOOK_PAYLOAD,
            headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret-xyz"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert captured["user_id"] == "777"
    assert captured["channel"] == "telegram"
    assert captured["org_id"] == 1


@pytest.mark.asyncio
async def test_telegram_webhook_org_not_found(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    llm_calls = 0

    async def fake_resolve(_token: str):
        return None

    async def fake_ai(**_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "reply"

    monkeypatch.setattr(telegram_api, "resolve_org_for_telegram_webhook", fake_resolve)
    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/bot/webhook",
            json={"update_id": 3001, "message": {"chat": {"id": 777}, "text": "hello"}},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "error", "detail": "org_not_found"}
    assert llm_calls == 0


@pytest.mark.asyncio
async def test_telegram_webhook_passes_org_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    org = _fake_org(org_id=99)
    _mock_org_resolve(monkeypatch, org)
    captured = {}

    async def fake_ai(*, user_id, user_text, db_memory, channel, org_id=None):
        captured["org_id"] = org_id
        return "reply"

    monkeypatch.setattr(telegram_api, "claim_inbound_event_or_duplicate", _always_claim)
    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai)
    monkeypatch.setattr(telegram_api, "send_telegram_message", lambda *_a, **_k: True)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/bot/webhook",
            json={"update_id": 3002, "message": {"chat": {"id": 777}, "text": "hello"}},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert captured["org_id"] == 99


@pytest.mark.asyncio
async def test_telegram_duplicate_skips_llm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    _mock_org_resolve(monkeypatch)
    claim_calls = 0
    llm_calls = 0

    async def fake_claim(*_args, **_kwargs):
        nonlocal claim_calls
        claim_calls += 1
        return claim_calls == 1

    async def fake_ai(**_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "reply"

    monkeypatch.setattr(telegram_api, "claim_inbound_event_or_duplicate", fake_claim)
    monkeypatch.setattr(telegram_api, "get_ai_response", fake_ai)
    monkeypatch.setattr(telegram_api, "send_telegram_message", lambda *_a, **_k: True)

    payload = {"update_id": 9001, "message": {"chat": {"id": 777}, "text": "hello"}}
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        first = await client.post("/bot/webhook", json=payload)
        second = await client.post("/bot/webhook", json=payload)

    assert first.status_code == 200
    assert first.json()["status"] == "ok"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert llm_calls == 1

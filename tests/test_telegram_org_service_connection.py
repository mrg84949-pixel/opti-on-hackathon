from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services import telegram_org_service as tg_org


def _org(token: str | None = "bot-token"):
    return SimpleNamespace(id=1, telegram_bot_token=token, telegram_bot_token_hash=None)


@pytest.mark.asyncio
async def test_test_telegram_connection_no_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tg_org, "get_org_secret", lambda _org, _key: None)
    monkeypatch.setattr(tg_org, "expected_telegram_webhook_url", lambda: "https://api.example/bot/webhook")
    result = await tg_org.test_telegram_connection(_org(token=None))
    assert result.ok is False
    assert result.token_set is False


@pytest.mark.asyncio
async def test_test_telegram_connection_success_with_webhook(monkeypatch: pytest.MonkeyPatch):
    expected = "https://api.example/bot/webhook"
    monkeypatch.setattr(tg_org, "get_org_secret", lambda _org, _key: "tok")
    monkeypatch.setattr(tg_org, "expected_telegram_webhook_url", lambda: expected)

    async def fake_api(token, method, **params):
        if method == "getMe":
            return {"username": "clinic_bot"}
        if method == "getWebhookInfo":
            return {"url": expected}
        return {}

    monkeypatch.setattr(tg_org, "_telegram_api_get", fake_api)
    result = await tg_org.test_telegram_connection(_org())
    assert result.ok is True
    assert result.bot_username == "@clinic_bot"
    assert result.webhook_url == expected


@pytest.mark.asyncio
async def test_test_telegram_connection_invalid_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tg_org, "get_org_secret", lambda _org, _key: "bad")
    monkeypatch.setattr(tg_org, "expected_telegram_webhook_url", lambda: None)

    async def fake_api(token, method, **params):
        raise RuntimeError("Unauthorized")

    monkeypatch.setattr(tg_org, "_telegram_api_get", fake_api)
    result = await tg_org.test_telegram_connection(_org())
    assert result.ok is False
    assert result.token_set is True


@pytest.mark.asyncio
async def test_register_telegram_webhook_missing_expected_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tg_org, "get_org_secret", lambda _org, _key: "tok")
    monkeypatch.setattr(tg_org, "expected_telegram_webhook_url", lambda: None)
    result = await tg_org.register_telegram_webhook(_org())
    assert result.ok is False
    assert "webhook URL" in result.message


@pytest.mark.asyncio
async def test_register_telegram_webhook_success(monkeypatch: pytest.MonkeyPatch):
    expected = "https://api.example/bot/webhook"
    monkeypatch.setattr(tg_org, "get_org_secret", lambda _org, _key: "tok")
    monkeypatch.setattr(tg_org, "expected_telegram_webhook_url", lambda: expected)

    org_secret = tg_org.telegram_webhook_secret_for_org(1)

    async def fake_api(token, method, **params):
        if method == "setWebhook":
            assert params["url"] == expected
            assert params["secret_token"] == org_secret
            return {}
        if method == "getMe":
            return {"username": "bot"}
        if method == "getWebhookInfo":
            return {"url": expected}
        return {}

    monkeypatch.setattr(tg_org, "_telegram_api_get", fake_api)
    result = await tg_org.register_telegram_webhook(_org())
    assert result.ok is True
    assert "зарегистрирован" in result.message.lower()


def test_expected_telegram_webhook_url_explicit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tg_org.settings, "telegram_webhook_url", "https://explicit/webhook")
    monkeypatch.setattr(tg_org.settings, "backend_public_url", "")
    assert tg_org.expected_telegram_webhook_url() == "https://explicit/webhook"


def test_expected_telegram_webhook_url_from_backend_public(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tg_org.settings, "telegram_webhook_url", "")
    monkeypatch.setattr(tg_org.settings, "backend_public_url", "https://api.example/")
    assert tg_org.expected_telegram_webhook_url() == "https://api.example/bot/webhook"


@pytest.mark.asyncio
async def test_send_telegram_auth_error(monkeypatch: pytest.MonkeyPatch):
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            return SimpleNamespace(status_code=401, is_success=False, text="Unauthorized")

    monkeypatch.setattr(tg_org.httpx, "AsyncClient", lambda timeout=20: _Client())
    org = _org(token="tok")
    result = await tg_org.send_telegram_for_org(org, 1, "hi")
    assert result.ok is False


@pytest.mark.asyncio
async def test_send_telegram_non_numeric_chat_id_returns_error(monkeypatch: pytest.MonkeyPatch):
    posted = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            posted.append(json)
            return SimpleNamespace(status_code=200, is_success=True, text="ok")

    monkeypatch.setattr(tg_org, "get_org_secret", lambda _org, _key: "tok")
    monkeypatch.setattr(tg_org.httpx, "AsyncClient", lambda timeout=20: _Client())
    org = _org(token="tok")
    result = await tg_org.send_telegram_for_org(org, "pilot-smoke", "hi")
    assert result.ok is False
    assert posted == []


@pytest.mark.asyncio
async def test_send_telegram_string_numeric_chat_id(monkeypatch: pytest.MonkeyPatch):
    posted = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            posted.append(json)
            return SimpleNamespace(status_code=200, is_success=True, text="ok")

    monkeypatch.setattr(tg_org, "get_org_secret", lambda _org, _key: "tok")
    monkeypatch.setattr(tg_org.httpx, "AsyncClient", lambda timeout=20: _Client())
    org = _org(token="tok")
    result = await tg_org.send_telegram_for_org(org, "900000001", "hi")
    assert result.ok is True
    assert posted == [{"chat_id": 900000001, "text": "hi"}]

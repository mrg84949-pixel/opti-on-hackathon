from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import errors as genai_errors

from bot.llm.context import TurnContext
from bot.llm.client_messages import AI_RATE_LIMIT, AI_UNAVAILABLE
from bot.llm.providers.gemini import GeminiProvider, GeminiSession
from bot.llm.providers.base import SessionSetup


def test_credentials_configured_when_client_exists(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.llm.providers.gemini.gemini.get_client", lambda: object())
    provider = GeminiProvider()
    assert provider.credentials_configured() is True


def test_credentials_configured_false_without_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.llm.providers.gemini.gemini.get_client", lambda: None)
    monkeypatch.setattr("bot.config.settings.gemini_api_key", "")
    provider = GeminiProvider()
    assert provider.credentials_configured() is False
    assert "GEMINI_API_KEY" in provider.missing_credentials_message()


def test_user_message_for_quota_error():
    provider = GeminiProvider()

    class _QuotaError(genai_errors.ClientError):
        def __str__(self):
            return "RESOURCE_EXHAUSTED code 429"

    msg = provider.user_message_for_error(_QuotaError(429, {}, None))
    assert msg == AI_RATE_LIMIT
    assert "gemini" not in msg.lower()
    assert "api_key" not in msg.lower()


def test_user_message_for_invalid_api_key():
    provider = GeminiProvider()

    class _KeyError(genai_errors.ClientError):
        def __str__(self):
            return "API key not valid"

    msg = provider.user_message_for_error(_KeyError(400, {}, None))
    assert msg == AI_UNAVAILABLE
    assert "GEMINI" not in msg


def test_user_message_for_generic_error():
    provider = GeminiProvider()
    assert provider.user_message_for_error(RuntimeError("boom")) == AI_UNAVAILABLE


@pytest.mark.asyncio
async def test_create_session_and_send_turn(monkeypatch: pytest.MonkeyPatch):
    chat = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(text="  hello  ")),
    )
    aio = SimpleNamespace(chats=SimpleNamespace(create=lambda **kwargs: chat))
    client = SimpleNamespace(aio=aio)
    monkeypatch.setattr("bot.llm.providers.gemini.gemini.get_client", lambda: client)
    monkeypatch.setattr("bot.llm.providers.gemini.debug_log", lambda **kwargs: None)

    provider = GeminiProvider(model="gemini-test")
    setup = SessionSetup(system_instruction="sys", tools=[], ctx=TurnContext(org_id=1))
    session = await provider.create_session(setup)
    assert isinstance(session, GeminiSession)
    text = await provider.send_turn(session, "hi")
    assert text == "hello"


@pytest.mark.asyncio
async def test_create_session_raises_when_client_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.llm.providers.gemini.gemini.get_client", lambda: None)
    provider = GeminiProvider()
    with pytest.raises(RuntimeError, match="unavailable"):
        await provider.create_session(SessionSetup(system_instruction="s", tools=[], ctx=TurnContext(org_id=1)))


class _QuotaError(genai_errors.ClientError):
    def __str__(self):
        return "RESOURCE_EXHAUSTED code 429"


@pytest.fixture(autouse=True)
def _fast_gemini_retries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.gemini_max_retries", 2)
    monkeypatch.setattr("bot.config.settings.gemini_retry_base_delay", 0.01)
    monkeypatch.setattr("bot.config.settings.gemini_retry_max_delay", 0.02)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("bot.llm.providers.gemini.asyncio.sleep", _no_sleep)


@pytest.mark.asyncio
async def test_send_turn_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    send_message = AsyncMock(
        side_effect=[_QuotaError(429, {}), SimpleNamespace(text="ok")]
    )
    session = GeminiSession(chat=SimpleNamespace(send_message=send_message))
    monkeypatch.setattr("bot.llm.providers.gemini.debug_log", lambda **kwargs: None)

    provider = GeminiProvider(model="gemini-test")
    text = await provider.send_turn(session, "hi")
    assert text == "ok"
    assert send_message.call_count == 2


@pytest.mark.asyncio
async def test_send_turn_persistent_rate_limit_reraises(monkeypatch: pytest.MonkeyPatch):
    send_message = AsyncMock(side_effect=_QuotaError(429, {}))
    session = GeminiSession(chat=SimpleNamespace(send_message=send_message))
    monkeypatch.setattr("bot.llm.providers.gemini.debug_log", lambda **kwargs: None)

    provider = GeminiProvider(model="gemini-test")
    with pytest.raises(genai_errors.ClientError):
        await provider.send_turn(session, "hi")
    # gemini_max_retries=2 -> 3 attempts total
    assert send_message.call_count == 3


@pytest.mark.asyncio
async def test_send_turn_non_rate_limit_error_not_retried(monkeypatch: pytest.MonkeyPatch):
    send_message = AsyncMock(side_effect=ValueError("boom"))
    session = GeminiSession(chat=SimpleNamespace(send_message=send_message))
    monkeypatch.setattr("bot.llm.providers.gemini.debug_log", lambda **kwargs: None)

    provider = GeminiProvider(model="gemini-test")
    with pytest.raises(ValueError):
        await provider.send_turn(session, "hi")
    assert send_message.call_count == 1

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.config import settings
from bot.llm.client_messages import AI_RATE_LIMIT, AI_UNAVAILABLE
from bot.llm.providers.openai_compat import OpenAICompatProvider, OpenAISession


class _RateLimit(Exception):
    status_code = 429


class _Msg:
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Resp:
    def __init__(self, content):
        self.choices = [SimpleNamespace(message=_Msg(content))]


class _FakeCreate:
    def __init__(self, side_effects):
        self._effects = list(side_effects)
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        effect = self._effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        return effect


def _session(create: _FakeCreate) -> OpenAISession:
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return OpenAISession(
        client=client,
        model="test-model",
        messages=[{"role": "system", "content": "sys"}],
        tools=[],
        tool_registry={},
    )


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(settings, "groq_max_retries", 2)
    monkeypatch.setattr(settings, "groq_retry_base_delay", 0.01)
    monkeypatch.setattr(settings, "groq_retry_max_delay", 0.02)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("bot.llm.providers.openai_compat.asyncio.sleep", _no_sleep)


async def test_retry_then_success():
    create = _FakeCreate([_RateLimit("rate_limit exceeded"), _Resp("готово")])
    provider = OpenAICompatProvider(api_key="k", model="test-model")
    out = await provider.send_turn(_session(create), "привет")
    assert out == "готово"
    assert create.calls == 2


async def test_persistent_rate_limit_reraises_and_maps_message():
    create = _FakeCreate([_RateLimit("429 too many requests")] * 5)
    provider = OpenAICompatProvider(api_key="k", model="test-model")
    with pytest.raises(_RateLimit) as excinfo:
        await provider.send_turn(_session(create), "привет")
    # groq_max_retries=2 -> 3 attempts total
    assert create.calls == 3
    assert provider.user_message_for_error(excinfo.value) == AI_RATE_LIMIT


async def test_non_rate_limit_error_not_retried():
    create = _FakeCreate([ValueError("boom")])
    provider = OpenAICompatProvider(api_key="k", model="test-model")
    with pytest.raises(ValueError):
        await provider.send_turn(_session(create), "привет")
    assert create.calls == 1


async def test_retry_after_header_is_used(monkeypatch):
    seen: list[float] = []

    async def _record_sleep(seconds):
        seen.append(seconds)

    # Raise the safety ceiling so the server-provided Retry-After (1.5s) is not capped.
    monkeypatch.setattr(settings, "groq_retry_max_delay", 5.0)
    monkeypatch.setattr("bot.llm.providers.openai_compat.asyncio.sleep", _record_sleep)

    class _RLWithHeader(Exception):
        status_code = 429
        response = SimpleNamespace(headers={"retry-after": "1.5"})

    create = _FakeCreate([_RLWithHeader("slow down"), _Resp("ок")])
    provider = OpenAICompatProvider(api_key="k", model="test-model")
    out = await provider.send_turn(_session(create), "привет")
    assert out == "ок"
    assert seen == [1.5]


def test_user_message_for_error_non_rate_is_unavailable():
    provider = OpenAICompatProvider(api_key="k", model="test-model")
    assert provider.user_message_for_error(ValueError("weird")) == AI_UNAVAILABLE


class _ToolUseFailed(Exception):
    status_code = 400
    body = {
        "code": "tool_use_failed",
        "failed_generation": "Здравствуйте! Чем могу помочь?\n<function=set_customer_name></function>",
    }


async def test_tool_use_failed_retries_then_uses_failed_generation():
    create = _FakeCreate(
        [
            _ToolUseFailed("tool call validation failed"),
            _ToolUseFailed("tool call validation failed"),
        ]
    )
    provider = OpenAICompatProvider(api_key="k", model="test-model")
    out = await provider.send_turn(_session(create), "привет")
    assert out == "Здравствуйте! Чем могу помочь?"
    assert create.calls == 2


async def test_tool_use_failed_retry_succeeds():
    create = _FakeCreate([_ToolUseFailed("tool call validation failed"), _Resp("готово")])
    provider = OpenAICompatProvider(api_key="k", model="test-model")
    out = await provider.send_turn(_session(create), "привет")
    assert out == "готово"
    assert create.calls == 2


def test_strip_function_markup_no_gt():
    from bot.llm.providers.openai_compat import strip_function_markup

    raw = '<function=set_customer_name{"name": "Клиент"}</function>'
    assert strip_function_markup(raw) == ""
    mixed = 'Hi <function=set_customer_name{"name": "X"}</function> there'
    cleaned = strip_function_markup(mixed)
    assert "function" not in cleaned.lower()
    assert "Hi" in cleaned
    assert "there" in cleaned

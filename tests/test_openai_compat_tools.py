from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bot.llm.context import TurnContext
from bot.llm.providers.openai_compat import OpenAICompatProvider
from bot.llm.providers.base import SessionSetup
from bot.llm.tool_schema import build_openai_tools, execute_tool_call


async def _sample_tool(name: str) -> str:
    """Sample tool for tests.

    Args:
        name: Customer name.
    """
    return f"saved:{name}"


@pytest.mark.asyncio
async def test_execute_tool_call_runs_async_tool():
    registry = {"set_customer_name": _sample_tool}
    result = await execute_tool_call(registry, "set_customer_name", json.dumps({"name": "Aliya"}))
    assert result == "saved:Aliya"


def test_build_openai_tools_includes_function_schema():
    tools = build_openai_tools([_sample_tool])
    assert len(tools) == 1
    fn = tools[0]["function"]
    assert fn["name"] == "_sample_tool"
    assert "name" in fn["parameters"]["properties"]


class _FakeMessage:
    def __init__(self, *, content: str | None = None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeCompletion:
    def __init__(self, message):
        self.choices = [_FakeChoice(message)]


class _FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        message = self.responses.pop(0)
        return _FakeCompletion(message)


class _FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))


@pytest.mark.asyncio
async def test_openai_compat_tool_loop(monkeypatch: pytest.MonkeyPatch):
    tool_call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="_sample_tool", arguments='{"name": "Aliya"}'),
    )
    responses = [
        _FakeMessage(content=None, tool_calls=[tool_call]),
        _FakeMessage(content="Done, Aliya"),
    ]
    fake_client = _FakeClient(responses)
    provider = OpenAICompatProvider(api_key="test", base_url="https://example.com/v1", model="test-model")
    monkeypatch.setattr(provider, "_client", lambda: fake_client)

    ctx = TurnContext(org_id=1, customer_id=1, services_catalog="")
    setup = SessionSetup(system_instruction="sys", tools=[_sample_tool], ctx=ctx)
    session = await provider.create_session(setup)
    reply = await provider.send_turn(session, "hello")

    assert reply == "Done, Aliya"
    assert fake_client.chat.completions.calls == 2
    assert session.messages[-1]["role"] == "assistant"
    assert session.messages[-2]["role"] == "tool"


def test_groq_user_message_for_quota_error():
    provider = OpenAICompatProvider(api_key="test")
    exc = Exception("rate_limit exceeded 429")
    exc.status_code = 429  # type: ignore[attr-defined]
    msg = provider.user_message_for_error(exc)
    assert "нагрузк" in msg.lower()
    assert "groq" not in msg.lower()
    assert "stub" not in msg.lower()


def test_groq_user_message_for_auth_error():
    provider = OpenAICompatProvider(api_key="test")
    exc = Exception("invalid api key")
    exc.status_code = 401  # type: ignore[attr-defined]
    msg = provider.user_message_for_error(exc)
    assert "groq" not in msg.lower()
    assert "api_key" not in msg.lower()


def test_groq_user_message_for_generic_error():
    provider = OpenAICompatProvider(api_key="test")
    msg = provider.user_message_for_error(RuntimeError("boom"))
    assert "groq" not in msg.lower()
    assert "boom" not in msg

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import bot.llm.llm_engine as llm_engine
from bot.billing_access import BOT_PAUSED_MESSAGE, TARIFF_BLOCKED_MESSAGE
from bot.llm.client_messages import AI_CONFIG_ERROR, AI_UNAVAILABLE
from bot.llm.context import TurnContext
from bot.llm.providers.base import SessionSetup


class _FakeSession:
    def __init__(self, *, get_results=None):
        self._get_results = dict(get_results or {})

    async def get(self, model, key):
        return self._get_results.get((model, key))


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SequentialSessionFactory:
    def __init__(self, sessions):
        self.sessions = list(sessions)

    def __call__(self):
        if not self.sessions:
            raise AssertionError("Unexpected AsyncSessionLocal() call")
        return _FakeSessionManager(self.sessions.pop(0))


class _RepeatingSessionFactory:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return _FakeSessionManager(self.session)


class _FakeProviderSession:
    def __init__(self, *, reply="ok", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.messages: list[str] = []


class _FakeLLMProvider:
    provider_name = "gemini"

    def __init__(self, *, reply="ok", error: Exception | None = None):
        self.reply = reply
        self.error = error
        self.create_calls = 0
        self.session: _FakeProviderSession | None = None
        self.last_setup: SessionSetup | None = None

    def credentials_configured(self) -> bool:
        return True

    def missing_credentials_message(self) -> str:
        return "missing credentials"

    async def create_session(self, setup: SessionSetup) -> _FakeProviderSession:
        self.create_calls += 1
        self.last_setup = setup
        self.session = _FakeProviderSession(reply=self.reply, error=self.error)
        return self.session

    async def send_turn(self, session: _FakeProviderSession, user_text: str) -> str:
        session.messages.append(user_text)
        if session.error is not None:
            raise session.error
        return session.reply

    def user_message_for_error(self, exc: Exception) -> str:
        text = str(exc)
        if "RESOURCE_EXHAUSTED" in text:
            return "превышен лимит Gemini API"
        if "API key not valid" in text:
            return "Некорректный GEMINI_API_KEY"
        return "Gemini временно недоступен"


class _StubOnlyProvider(_FakeLLMProvider):
    provider_name = "stub"

    def credentials_configured(self) -> bool:
        return True

    async def send_turn(self, session: Any, user_text: str) -> str:
        return "stubbed"


class _MissingCredentialsProvider(_FakeLLMProvider):
    def credentials_configured(self) -> bool:
        return False

    def missing_credentials_message(self) -> str:
        return "GEMINI_API_KEY missing"


def _org(org_id=1, **overrides):
    base = {
        "id": org_id,
        "system_prompt": "",
        "timezone": "UTC",
        "billing_paid_until": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _patch_customer_channel_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_customer(*args, **kwargs):
        return SimpleNamespace(id=77, muted_until=None, dialog_context={})

    async def fake_maybe_auto_mute(session, customer):
        return False

    async def fake_resolve_tool_mode(session, org_id, customer):
        return "booking"

    async def fake_booking_shortcut(**_kwargs):
        return None

    async def fake_booking_fsm(**_kwargs):
        return None

    monkeypatch.setattr(llm_engine, "get_or_create_customer_for_channel", fake_customer)
    monkeypatch.setattr(llm_engine.customer_service, "maybe_auto_mute", fake_maybe_auto_mute)
    monkeypatch.setattr(llm_engine, "resolve_tool_mode", fake_resolve_tool_mode)
    monkeypatch.setattr(
        llm_engine.booking_deterministic,
        "try_handle_booking_shortcut",
        fake_booking_shortcut,
    )
    monkeypatch.setattr(
        llm_engine.booking_fsm,
        "try_handle_booking_fsm",
        fake_booking_fsm,
    )


@pytest.mark.asyncio
async def test_get_ai_response_org_missing(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): None})),
    )
    result = await llm_engine.get_ai_response("u1", "hello", {})
    assert result == AI_CONFIG_ERROR
    assert ".env" not in result


@pytest.mark.asyncio
async def test_get_ai_response_billing_blocked(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    blocked_org = _org(billing_paid_until=datetime.now(timezone.utc) - timedelta(days=1))
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): blocked_org})),
    )
    captured = {}

    async def fake_persist(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)
    result = await llm_engine.get_ai_response("u1", "hello", {})
    assert result == TARIFF_BLOCKED_MESSAGE
    assert captured["status"] == "billing_blocked"
    assert captured["reply"] == TARIFF_BLOCKED_MESSAGE


@pytest.mark.asyncio
async def test_get_ai_response_bot_paused(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    paused_org = _org(bot_enabled=False)
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): paused_org})),
    )
    captured = {}

    async def fake_persist(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)
    result = await llm_engine.get_ai_response("u1", "hello", {})
    assert result == BOT_PAUSED_MESSAGE
    assert captured["status"] == "bot_paused"
    assert captured["reply"] == BOT_PAUSED_MESSAGE


@pytest.mark.asyncio
async def test_get_ai_response_stub_missing_key_and_no_client(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    active_org = _org()
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): active_org})),
    )

    async def fake_persist(**kwargs):
        return None

    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)
    _patch_customer_channel_mocks(monkeypatch)

    monkeypatch.setenv("AI_USE_STUB", "1")
    monkeypatch.setenv("AI_STUB_MESSAGE", "stubbed")
    monkeypatch.setattr(llm_engine, "get_llm_provider", lambda org=None: _StubOnlyProvider())
    stub_result = await llm_engine.get_ai_response("u1", "hello", {})
    assert stub_result == "stubbed"

    monkeypatch.setenv("AI_USE_STUB", "0")
    monkeypatch.setattr(llm_engine, "get_llm_provider", lambda org=None: _MissingCredentialsProvider())
    missing_key = await llm_engine.get_ai_response("u1", "hello", {})
    assert missing_key == AI_UNAVAILABLE
    assert "GEMINI_API_KEY" not in missing_key

    class _NoClientProvider(_MissingCredentialsProvider):
        def credentials_configured(self) -> bool:
            return False

        def missing_credentials_message(self) -> str:
            return "не удалось создать клиент Gemini"

    monkeypatch.setattr(llm_engine, "get_llm_provider", lambda org=None: _NoClientProvider())
    no_client = await llm_engine.get_ai_response("u1", "hello", {})
    assert no_client == AI_UNAVAILABLE
    assert "Gemini" not in no_client


@pytest.mark.asyncio
async def test_get_ai_response_success_and_session_reuse(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    active_org = _org(system_prompt="Clinic prompt")
    shared_session = _FakeSession(get_results={(llm_engine.Organization, 1): active_org})
    monkeypatch.setattr(llm_engine, "AsyncSessionLocal", _RepeatingSessionFactory(shared_session))
    monkeypatch.setenv("AI_USE_STUB", "0")
    persisted = []

    async def fake_persist(**kwargs):
        persisted.append(kwargs)

    fake_provider = _FakeLLMProvider(reply="hello-from-assistant")
    monkeypatch.setattr(llm_engine, "get_llm_provider", lambda org=None: fake_provider)
    _patch_customer_channel_mocks(monkeypatch)

    async def fake_catalog(*args, **kwargs):
        return "catalog"
    monkeypatch.setattr(llm_engine, "load_services_catalog_for_org", fake_catalog)
    async def fake_tool():
        return "ok"

    monkeypatch.setattr(llm_engine, "make_tools", lambda ctx, mode=None: [fake_tool])
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)

    first = await llm_engine.get_ai_response("u1", "hello", {})
    second = await llm_engine.get_ai_response("u1", "again", {})
    assert first == "hello-from-assistant"
    assert second == "hello-from-assistant"
    assert fake_provider.create_calls == 1
    assert fake_provider.session is not None
    assert fake_provider.session.messages == ["hello", "again"]
    assert fake_provider.last_setup is not None
    assert "Clinic prompt" in fake_provider.last_setup.system_instruction
    assert "Платформенные правила" in fake_provider.last_setup.system_instruction
    assert "не должны противоречить платформенным правилам" in fake_provider.last_setup.system_instruction


@pytest.mark.asyncio
async def test_get_ai_response_recreates_session_on_prompt_version_change(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    active_org = _org()
    shared_session = _FakeSession(get_results={(llm_engine.Organization, 1): active_org})
    monkeypatch.setattr(llm_engine, "AsyncSessionLocal", _RepeatingSessionFactory(shared_session))
    monkeypatch.setenv("AI_USE_STUB", "0")

    async def fake_persist(**_kwargs):
        return None

    fake_provider = _FakeLLMProvider(reply="hello-from-assistant")
    monkeypatch.setattr(llm_engine, "get_llm_provider", lambda org=None: fake_provider)
    _patch_customer_channel_mocks(monkeypatch)

    async def fake_catalog(*_args, **_kwargs):
        return "catalog"

    monkeypatch.setattr(llm_engine, "load_services_catalog_for_org", fake_catalog)

    async def fake_tool():
        return "ok"

    monkeypatch.setattr(llm_engine, "make_tools", lambda ctx, mode=None: [fake_tool])
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)

    first = await llm_engine.get_ai_response("u1", "hello", {})
    assert first == "hello-from-assistant"
    assert fake_provider.create_calls == 1

    key = llm_engine._session_key("web", "u1", 1)
    llm_engine._sessions[key] = llm_engine._UserSession(
        handle=fake_provider.session,
        provider_name="gemini",
        runtime_fingerprint="gemini|old-model|booking-ux-old",
        tool_mode="booking",
    )

    second = await llm_engine.get_ai_response("u1", "again", {})
    assert second == "hello-from-assistant"
    assert fake_provider.create_calls == 2


@pytest.mark.asyncio
async def test_get_ai_response_muted_customer(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    now = datetime.now(timezone.utc)
    muted_customer = SimpleNamespace(
        id=77,
        muted_until=now + timedelta(days=7),
        dialog_context={},
    )

    async def fake_customer(*args, **kwargs):
        return muted_customer

    async def fake_maybe_auto_mute(session, customer):
        return False

    monkeypatch.setattr(llm_engine, "get_or_create_customer_for_channel", fake_customer)
    monkeypatch.setattr(llm_engine.customer_service, "maybe_auto_mute", fake_maybe_auto_mute)
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): _org()})),
    )
    captured: dict[str, object] = {}

    async def fake_persist(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)

    result = await llm_engine.get_ai_response("u1", "hello after handoff", {})
    assert result == llm_engine.customer_service.MUTE_USER_MESSAGE
    assert captured["status"] == "muted"
    assert captured["reply"] == llm_engine.customer_service.MUTE_USER_MESSAGE


@pytest.mark.asyncio
async def test_build_session_includes_context_summary(monkeypatch: pytest.MonkeyPatch):
    summary_text = "Клиент Ivan записан на консультацию завтра в 10:00."

    async def fake_customer(*args, **kwargs):
        return SimpleNamespace(
            id=77,
            muted_until=None,
            dialog_context={"context_summary": summary_text},
        )

    async def fake_resolve(_session, org_id, customer):
        return "booking"

    monkeypatch.setattr(llm_engine, "get_or_create_customer_for_channel", fake_customer)
    monkeypatch.setattr(llm_engine, "resolve_tool_mode", fake_resolve)

    async def fake_catalog(*_a, **_k):
        return "cat"

    monkeypatch.setattr(llm_engine, "load_services_catalog_for_org", fake_catalog)
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): _org()})),
    )

    setup = await llm_engine._build_session_setup(
        target_org_id=1, channel="web", user_id="u1"
    )
    assert "Краткий контекст клиента" in setup.system_instruction
    assert "Ivan" in setup.system_instruction
    assert "Платформенные правила" in setup.system_instruction


@pytest.mark.asyncio
@pytest.mark.parametrize("error_text", ["RESOURCE_EXHAUSTED", "API key not valid", "something else"])
async def test_get_ai_response_error_mapping(monkeypatch: pytest.MonkeyPatch, error_text: str):
    llm_engine._sessions.clear()
    active_org = _org()
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): active_org})),
    )
    monkeypatch.setenv("AI_USE_STUB", "0")

    class FakeClientError(Exception):
        pass

    fake_provider = _FakeLLMProvider(error=FakeClientError(error_text))
    monkeypatch.setattr(llm_engine, "get_llm_provider", lambda org=None: fake_provider)
    key = llm_engine._session_key("web", "u1", 1)
    llm_engine._sessions[key] = llm_engine._UserSession(
        handle=_FakeProviderSession(error=FakeClientError(error_text)),
        provider_name="gemini",
    )
    captured = {}

    async def fake_persist(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)
    _patch_customer_channel_mocks(monkeypatch)

    async def fake_catalog(*_a, **_k):
        return "catalog"

    monkeypatch.setattr(llm_engine, "load_services_catalog_for_org", fake_catalog)
    result = await llm_engine.get_ai_response("u1", "hello", {}, channel="web", org_id=1)
    assert result == AI_UNAVAILABLE
    assert "gemini" not in result.lower()
    assert captured["status"] == "error"
    assert captured["reply"] == AI_UNAVAILABLE
    assert captured["error_hint"]


@pytest.mark.asyncio
async def test_get_ai_response_sanitizes_llm_parrot(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    active_org = _org()
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        _RepeatingSessionFactory(_FakeSession(get_results={(llm_engine.Organization, 1): active_org})),
    )
    monkeypatch.setenv("AI_USE_STUB", "0")

    async def fake_persist(**_kwargs):
        return None

    fake_provider = _FakeLLMProvider(reply="Please switch AI_PROVIDER=stub for tests")
    monkeypatch.setattr(llm_engine, "get_llm_provider", lambda org=None: fake_provider)
    _patch_customer_channel_mocks(monkeypatch)

    async def fake_catalog(*_a, **_k):
        return "catalog"

    monkeypatch.setattr(llm_engine, "load_services_catalog_for_org", fake_catalog)
    monkeypatch.setattr(llm_engine, "make_tools", lambda ctx, mode=None: [])
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)

    result = await llm_engine.get_ai_response("u1", "hello", {}, org_id=1)
    assert result == AI_UNAVAILABLE


@pytest.mark.asyncio
async def test_get_ai_response_booking_shortcut_skips_llm(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    active_org = _org()
    monkeypatch.setattr(
        llm_engine,
        "AsyncSessionLocal",
        _RepeatingSessionFactory(_FakeSession(get_results={(llm_engine.Organization, 1): active_org})),
    )
    monkeypatch.setenv("AI_USE_STUB", "0")
    persisted = []

    async def fake_persist(**kwargs):
        persisted.append(kwargs)

    fake_provider = _FakeLLMProvider(reply="should-not-run")
    monkeypatch.setattr(llm_engine, "get_llm_provider", lambda org=None: fake_provider)
    _patch_customer_channel_mocks(monkeypatch)
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)

    async def fake_shortcut(**_kwargs):
        return ("Каталог услуг", "booking_shortcut")

    monkeypatch.setattr(
        llm_engine.booking_deterministic,
        "try_handle_booking_shortcut",
        fake_shortcut,
    )

    result = await llm_engine.get_ai_response("u1", "какие услуги", {}, org_id=1)
    assert result == "Каталог услуг"
    assert fake_provider.create_calls == 0
    assert persisted[-1]["status"] == "booking_shortcut"


def test_clear_in_memory_session_removes_key():
    key = llm_engine._session_key("web", "admin-sandbox-testid12", 1)
    llm_engine._sessions[key] = llm_engine._UserSession(handle=object(), provider_name="stub")
    llm_engine.clear_in_memory_session(channel="web", user_id="admin-sandbox-testid12", org_id=1)
    assert key not in llm_engine._sessions

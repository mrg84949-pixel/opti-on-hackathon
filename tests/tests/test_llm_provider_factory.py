from __future__ import annotations

import pytest

from bot.config import settings
from bot.db.models import Organization
from bot.llm.providers.factory import get_llm_provider
from bot.llm.providers.gemini import GeminiProvider
from bot.llm.providers.openai_compat import OpenAICompatProvider
from bot.llm.providers.stub import StubProvider


def test_factory_returns_stub_when_ai_use_stub(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_USE_STUB", "1")
    monkeypatch.setenv("AI_PROVIDER", "gemini")
    provider = get_llm_provider()
    assert isinstance(provider, StubProvider)


def test_factory_returns_stub_provider_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_USE_STUB", "0")
    monkeypatch.setenv("AI_PROVIDER", "stub")
    provider = get_llm_provider()
    assert isinstance(provider, StubProvider)
    assert provider.provider_name == "stub"


def test_factory_returns_groq(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_USE_STUB", "0")
    monkeypatch.setenv("AI_PROVIDER", "groq")
    provider = get_llm_provider()
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.provider_name == "groq"


def test_factory_defaults_to_gemini(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_USE_STUB", "0")
    monkeypatch.delenv("AI_PROVIDER", raising=False)
    object.__setattr__(settings, "ai_provider", "gemini")
    provider = get_llm_provider()
    assert isinstance(provider, GeminiProvider)


def test_factory_uses_org_provider_and_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AI_USE_STUB", "0")
    org = Organization(name="Demo")
    org.ai_provider = "groq"
    org.ai_config = {"model": "llama-org"}
    provider = get_llm_provider(org)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider._model == "llama-org"


def test_factory_org_gemini_model_override():
    org = Organization(name="Demo")
    org.ai_provider = "gemini"
    org.ai_config = {"model": "gemini-custom"}
    provider = get_llm_provider(org)
    assert isinstance(provider, GeminiProvider)
    assert provider._model == "gemini-custom"

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.config import settings
from bot.db.models import Organization
from bot.llm.prompts import PROMPT_VERSION
from bot.llm.providers.registry import (
    merge_ai_config,
    platform_credentials_ready,
    public_ai_config,
    resolve_ai_model,
    resolve_ai_provider_key,
    runtime_fingerprint,
)


def _org(**kwargs) -> Organization:
    org = Organization(name="Clinic")
    org.id = 1
    for key, value in kwargs.items():
        setattr(org, key, value)
    return org


def test_resolve_ai_provider_key_falls_back_to_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ai_provider", "gemini")
    org = _org(ai_provider=None)
    assert resolve_ai_provider_key(org) == "gemini"
    assert resolve_ai_provider_key(None) == "gemini"


def test_resolve_ai_provider_key_uses_org_override():
    org = _org(ai_provider="groq")
    assert resolve_ai_provider_key(org) == "groq"


def test_resolve_ai_model_uses_org_config():
    org = _org(ai_provider="gemini", ai_config={"model": "custom-model"})
    assert resolve_ai_model(org, "gemini") == "custom-model"


def test_merge_ai_config_and_public():
    org = _org()
    merge_ai_config(org, {"model": " gemini-x "})
    assert public_ai_config(org) == {"model": "gemini-x"}
    merge_ai_config(org, {"model": None})
    assert public_ai_config(org) == {}


def test_runtime_fingerprint():
    org = _org(ai_provider="groq", ai_config={"model": "llama-test"})
    assert runtime_fingerprint(org) == f"groq|llama-test|{PROMPT_VERSION}"


def test_platform_credentials_ready_stub():
    assert platform_credentials_ready("stub") is True


def test_platform_credentials_ready_gemini(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "gemini_api_key", "")
    assert platform_credentials_ready("gemini") is False
    monkeypatch.setattr(settings, "gemini_api_key", "key")
    assert platform_credentials_ready("gemini") is True

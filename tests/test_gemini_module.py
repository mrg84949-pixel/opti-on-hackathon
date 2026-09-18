from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.llm.gemini as gemini


def test_get_client_returns_none_without_key(monkeypatch: pytest.MonkeyPatch):
    gemini._client = None
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert gemini.get_client() is None


def test_get_client_creates_and_caches_client(monkeypatch: pytest.MonkeyPatch):
    gemini._client = None
    created = {"count": 0}

    def fake_client(*, api_key):
        created["count"] += 1
        return SimpleNamespace(api_key=api_key)

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(gemini.genai, "Client", fake_client)
    first = gemini.get_client()
    second = gemini.get_client()
    assert first.api_key == "test-key"
    assert second is first
    assert created["count"] == 1


def test_get_model_name_env_override(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert gemini.get_model_name() == "gemini-2.5-flash-lite"
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom")
    assert gemini.get_model_name() == "gemini-custom"

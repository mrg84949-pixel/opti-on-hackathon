from __future__ import annotations

from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

import main


class _FakeSession:
    def __init__(self, org_by_id: dict[int, object | None]):
        self._org_by_id = org_by_id

    async def get(self, _model, key):
        return self._org_by_id.get(key)


class _FakeSessionManager:
    def __init__(self, org_by_id: dict[int, object | None]):
        self._org_by_id = org_by_id

    async def __aenter__(self):
        return _FakeSession(self._org_by_id)

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_org_lookup(monkeypatch: pytest.MonkeyPatch, org_by_id: dict[int, object | None]):
    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: _FakeSessionManager(org_by_id))


@pytest.mark.asyncio
async def test_chat_strict_requires_org_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    llm_calls = 0

    async def fake_ai(**_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "reply"

    monkeypatch.setattr(main.llm_engine, "get_ai_response", fake_ai)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/chat", json={"user_message": "hello", "client_id": "u1"})

    assert response.status_code == 400
    assert "org_id is required" in response.json()["detail"]
    assert llm_calls == 0


@pytest.mark.asyncio
async def test_chat_strict_with_org_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    captured = {}

    async def fake_ai(**kwargs):
        captured.update(kwargs)
        return "reply"

    _patch_org_lookup(monkeypatch, {1: SimpleNamespace(id=1)})
    monkeypatch.setattr(main.llm_engine, "get_ai_response", fake_ai)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post(
            "/chat",
            json={"user_message": "hello", "client_id": "u1", "org_id": 1},
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "reply"
    assert captured["org_id"] == 1
    assert captured["channel"] == "web"


@pytest.mark.asyncio
async def test_chat_strict_unknown_org_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    llm_calls = 0

    async def fake_ai(**_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "reply"

    _patch_org_lookup(monkeypatch, {99: None})
    monkeypatch.setattr(main.llm_engine, "get_ai_response", fake_ai)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post(
            "/chat",
            json={"user_message": "hello", "client_id": "u1", "org_id": 99},
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "organization not found"
    assert llm_calls == 0


@pytest.mark.asyncio
async def test_chat_dev_fallback_default_org(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    monkeypatch.setattr("bot.config.settings.default_org_id", 3)
    captured = {}

    async def fake_ai(**kwargs):
        captured.update(kwargs)
        return "reply"

    _patch_org_lookup(monkeypatch, {3: SimpleNamespace(id=3)})
    monkeypatch.setattr(main.llm_engine, "get_ai_response", fake_ai)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post("/chat", json={"user_message": "hello", "client_id": "u1"})

    assert response.status_code == 200
    assert captured["org_id"] == 3


@pytest.mark.asyncio
async def test_chat_dev_explicit_org_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    monkeypatch.setattr("bot.config.settings.default_org_id", 1)
    captured = {}

    async def fake_ai(**kwargs):
        captured.update(kwargs)
        return "reply"

    _patch_org_lookup(monkeypatch, {7: SimpleNamespace(id=7)})
    monkeypatch.setattr(main.llm_engine, "get_ai_response", fake_ai)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        response = await client.post(
            "/chat",
            json={"user_message": "hello", "client_id": "u1", "org_id": 7},
        )

    assert response.status_code == 200
    assert captured["org_id"] == 7

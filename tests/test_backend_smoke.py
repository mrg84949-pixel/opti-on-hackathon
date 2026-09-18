from __future__ import annotations

from httpx import ASGITransport, AsyncClient
import pytest

import main


class _FakeSession:
    async def execute(self, _stmt):
        return 1


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_backend_smoke_routes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEFAULT_ORG_ID", "1")
    monkeypatch.setenv("USER_AUTH_SECRET", "super-secret")
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)

    class _OrgSession(_FakeSession):
        async def get(self, _model, key):
            if key == 1:
                return object()
            return None

    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: _FakeSessionManager(_OrgSession()))

    async def fake_ai_response(*, user_id, user_text, db_memory, channel, org_id=None):
        assert channel == "web"
        assert org_id == 1
        return f"echo:{user_text}"

    monkeypatch.setattr(main.llm_engine, "get_ai_response", fake_ai_response)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        root = await client.get("/")
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
        metrics = await client.get("/metrics")
        chat = await client.post("/chat", json={"user_message": "hello", "client_id": "smoke-user"})

    assert root.status_code == 200
    assert health.status_code == 200
    assert ready.status_code == 200
    assert ready.json()["status"] == "ok"
    assert metrics.status_code == 200
    assert "bot_http_requests_total" in metrics.text
    assert chat.status_code == 200
    assert chat.json()["reply"] == "echo:hello"

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.user_api as user_api


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(user_api.router, prefix="/api/web/user")
    return app


class _FakeResult:
    def scalar_one_or_none(self):
        return None


class _FakeSession:
    def __init__(self):
        self.added = []

    async def execute(self, _stmt):
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)
        if not getattr(obj, "id", None):
            obj.id = 5

    async def flush(self):
        return None

    async def commit(self):
        return None


class _FakeCtx:
    def __init__(self, s):
        self.s = s

    async def __aenter__(self):
        return self.s

    async def __aexit__(self, exc_type, exc, tb):
        return False


import pytest


@pytest.mark.asyncio
async def test_oauth_start_and_callback(monkeypatch: pytest.MonkeyPatch):
    fake_session = _FakeSession()
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeCtx(fake_session))
    app = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        start = await client.get("/api/web/user/auth/oauth/google/start")
        callback = await client.get("/api/web/user/auth/oauth/google/callback?code=test-code")
    assert start.status_code == 200
    assert callback.status_code == 200
    assert "session_id" in callback.json()

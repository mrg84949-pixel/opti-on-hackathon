from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, status
from httpx import ASGITransport, AsyncClient

import web.user_api as user_api


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False

    async def execute(self, _stmt):
        service = SimpleNamespace(id=1, slug="consultation", name="Consult", description="desc", price_minor=1000, currency="KZT")
        return _Result([service])

    async def get(self, _model, _id):
        return SimpleNamespace(id=1, is_active=True, price_minor=1000, currency="KZT")

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for idx, obj in enumerate(self.added, start=1):
            if not getattr(obj, "id", None):
                obj.id = idx

    async def commit(self):
        self.committed = True


class _FakeSessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(user_api.router, prefix="/api/web/user")
    return app


@pytest.mark.asyncio
async def test_services_public(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(_FakeSession()))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/api/web/user/services")
    assert response.status_code == 200
    assert response.json()["items"][0]["slug"] == "consultation"


@pytest.mark.asyncio
async def test_create_service_request_requires_auth(monkeypatch: pytest.MonkeyPatch):
    async def _fake_require_user(_session: str | None):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    monkeypatch.setattr(user_api, "_require_user", _fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(_FakeSession()))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post("/api/web/user/services/1/request", json={})
    assert response.status_code == 401

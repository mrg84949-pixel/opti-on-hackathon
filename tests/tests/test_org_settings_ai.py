from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Organization
from web.admin_auth import AdminAuth, get_admin_auth


class _FakeSession:
    def __init__(self, *, org: Organization | None):
        self._org = org
        self.committed = False

    async def get(self, model, key):
        if model is Organization and self._org is not None and self._org.id == key:
            return self._org
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, org):
        return None


class _FakeSessionManager:
    def __init__(self, session: _FakeSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers(*, org_id: int = 1) -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": str(org_id)}


@pytest.mark.asyncio
async def test_list_ai_providers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/ai-providers", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    codes = {item["code"] for item in payload}
    assert {"gemini", "groq", "stub"}.issubset(codes)


@pytest.mark.asyncio
async def test_org_settings_ai_patch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    fake_session = _FakeSession(org=org)

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=1)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch(
                "/api/web/org-settings",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
                json={"ai_provider": "groq", "ai_model": "llama-custom"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    payload = response.json()
    assert org.ai_provider == "groq"
    assert org.ai_config == {"model": "llama-custom"}
    assert payload["ai_provider_effective"] == "groq"
    assert payload["ai_model_effective"] == "llama-custom"
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_org_settings_ai_invalid_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    fake_session = _FakeSession(org=org)

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=1)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch(
                "/api/web/org-settings",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
                json={"ai_provider": "unknown"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Organization
from bot.services import whatsapp_org_service as wa_service


class _FakeSession:
    def __init__(self, *, org: Organization | None):
        self._org = org
        self.committed = False
        self.refreshed = False

    async def get(self, model, key):
        if model is Organization:
            return self._org if self._org is not None and self._org.id == key else None
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        self.refreshed = True


class _FakeSessionManager:
    def __init__(self, session: _FakeSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _org(**kwargs) -> Organization:
    org = Organization(
        id=1,
        name="Clinic A",
        whatsapp_provider="green",
        whatsapp_instance_id="inst-1",
        whatsapp_api_token="secret-wa-token",
        whatsapp_meta_reminder_template_lang="ru",
        whatsapp_broadcast_quota_monthly=500,
        whatsapp_broadcast_sent_count=0,
    )
    for key, value in kwargs.items():
        setattr(org, key, value)
    return org


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers(*, org_id: int = 1) -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": str(org_id)}


@pytest.mark.asyncio
async def test_whatsapp_settings_test_green_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    async def _success(_org):
        return wa_service.WhatsAppConnectionTestResult(
            ok=True,
            provider="green",
            message="Green-API: инстанс authorized.",
        )

    monkeypatch.setattr(admin_api, "test_whatsapp_connection", _success)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/whatsapp-settings/test", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["provider"] == "green"


@pytest.mark.asyncio
async def test_whatsapp_settings_test_meta_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(whatsapp_provider="meta", whatsapp_meta_phone_number_id="123", whatsapp_meta_access_token="tok")
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    async def _fail(_org):
        return wa_service.WhatsAppConnectionTestResult(
            ok=False,
            provider="meta",
            message="Meta: неверный access token.",
        )

    monkeypatch.setattr(admin_api, "test_whatsapp_connection", _fail)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/whatsapp-settings/test", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["provider"] == "meta"


@pytest.mark.asyncio
async def test_whatsapp_settings_put_token_rotated(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(whatsapp_api_token=None)
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/api/web/whatsapp-settings",
            headers=_auth_headers(),
            json={"whatsapp_api_token": "new-green-token"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["whatsapp_token_rotated"] is True
    assert payload["whatsapp_api_token_set"] is True
    assert "new-green-token" not in str(payload)

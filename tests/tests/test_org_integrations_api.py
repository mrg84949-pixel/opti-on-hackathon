from __future__ import annotations

from types import SimpleNamespace

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
        crm_provider="amocrm",
        crm_base_url="https://crm.example",
        crm_api_token="secret-crm-token",
        telegram_bot_token="secret-tg-token",
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
async def test_get_org_integrations_masks_secrets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/org-integrations", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["org_id"] == 1
    assert payload["crm_api_token_set"] is True
    assert payload["telegram_bot_token_set"] is True
    assert "crm_api_token" not in payload
    assert "telegram_bot_token" not in payload
    assert payload["crm_resolved_provider"] == "amocrm"
    assert payload["crm_without_external"] is False
    assert "telegram_send_healthy" in payload


@pytest.mark.asyncio
async def test_get_org_integrations_none_without_external(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(crm_provider="none", crm_base_url=None, crm_api_token=None)
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/org-integrations", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["crm_provider"] == "none"
    assert payload["crm_without_external"] is True
    assert payload["crm_demo_mode"] is True


@pytest.mark.asyncio
async def test_get_org_integrations_generic_rest_not_without_external(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(
        crm_provider="generic_rest",
        crm_base_url="https://rest.example",
        crm_api_token="secret",
    )
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/org-integrations", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["crm_provider"] == "generic_rest"
    assert payload["crm_resolved_provider"] == "generic_rest"
    assert payload["crm_without_external"] is False


@pytest.mark.asyncio
async def test_patch_org_integrations_writes_secrets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(crm_api_token=None, telegram_bot_token=None)
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/api/web/org-integrations",
            headers=_auth_headers(),
            json={
                "crm_provider": "demo",
                "crm_base_url": "https://new.example",
                "crm_api_token": "new-crm-token",
                "telegram_bot_token": "new-tg-token",
            },
        )

    assert response.status_code == 200
    assert session.committed is True
    assert org.crm_provider == "demo"
    assert org.crm_base_url == "https://new.example"
    assert org.crm_api_token is not None
    assert org.telegram_bot_token is not None
    payload = response.json()
    assert payload["crm_api_token_set"] is True
    assert payload["telegram_bot_token_set"] is True
    assert payload["telegram_token_rotated"] is True
    assert "new-crm-token" not in str(payload)
    assert "new-tg-token" not in str(payload)


@pytest.mark.asyncio
async def test_patch_org_integrations_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/api/web/org-integrations",
            headers=_auth_headers(),
            json={"crm_provider": "bitrix24"},
        )

    assert response.status_code == 400
    assert session.committed is False


@pytest.mark.asyncio
async def test_session_admin_cannot_patch_other_org_integrations(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(id=1)
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=10)

    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.patch(
                "/api/web/org-integrations",
                headers={**_auth_headers(org_id=2), "X-Admin-Session": "sess"},
                json={"crm_base_url": "https://evil.example"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert org.crm_base_url == "https://evil.example"
    assert org.id == 1


@pytest.mark.asyncio
async def test_list_org_integration_providers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/org-integrations/providers", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    codes = {item["code"] for item in payload}
    assert codes == {"amocrm", "demo", "none", "generic_rest", "yclients", "macdent"}
    amocrm = next(item for item in payload if item["code"] == "amocrm")
    field_names = {field["name"] for field in amocrm["fields"]}
    assert field_names == {"crm_base_url", "crm_api_token"}
    yclients = next(item for item in payload if item["code"] == "yclients")
    yclients_fields = {field["name"] for field in yclients["fields"]}
    assert yclients_fields == {"crm_api_token", "company_id", "default_service_id", "crm_user_token"}


@pytest.mark.asyncio
async def test_patch_org_integrations_writes_crm_user_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/api/web/org-integrations",
            headers=_auth_headers(),
            json={"crm_provider": "yclients", "crm_user_token": "user-secret"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["crm_user_token_set"] is True
    assert "crm_user_token" not in payload
    assert "user-secret" not in str(payload)


@pytest.mark.asyncio
async def test_patch_org_integrations_accepts_yclients(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/api/web/org-integrations",
            headers=_auth_headers(),
            json={"crm_provider": "yclients", "company_id": "4564", "crm_api_token": "partner-token"},
        )

    assert response.status_code == 200
    assert org.crm_provider == "yclients"
    assert org.crm_config == {"company_id": "4564"}
    payload = response.json()
    assert payload["crm_resolved_provider"] == "yclients"


@pytest.mark.asyncio
async def test_patch_org_integrations_accepts_yclients_default_service_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/api/web/org-integrations",
            headers=_auth_headers(),
            json={
                "crm_provider": "yclients",
                "company_id": "4564",
                "default_service_id": "331",
                "crm_api_token": "partner-token",
            },
        )

    assert response.status_code == 200
    assert org.crm_config == {"company_id": "4564", "default_service_id": "331"}
    payload = response.json()
    assert payload["crm_config"] == {"company_id": "4564", "default_service_id": "331"}


@pytest.mark.asyncio
async def test_patch_org_integrations_merges_crm_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(crm_config=None)
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/api/web/org-integrations",
            headers=_auth_headers(),
            json={"crm_config": {"staff_path": "/custom/staff", "crm_api_token": "ignored"}},
        )

    assert response.status_code == 200
    assert org.crm_config == {"staff_path": "/custom/staff"}
    assert response.json()["crm_config"] == {"staff_path": "/custom/staff"}


@pytest.mark.asyncio
async def test_post_test_crm_demo_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(crm_provider="demo")
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/org-integrations/test-crm", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["demo_mode"] is True
    assert payload["staff_count"] >= 1
    assert "без внешней CRM" in payload["message"]


@pytest.mark.asyncio
async def test_post_test_crm_failure_returns_ok_false(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(crm_provider="amocrm", crm_base_url="https://crm.example", crm_api_token="tok")

    class _FailingProvider:
        demo_mode = False

        async def list_staff(self):
            raise RuntimeError("crm unreachable")

    def _fake_provider(_org):
        return _FailingProvider()

    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))
    monkeypatch.setattr(admin_api, "get_crm_provider", _fake_provider)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/org-integrations/test-crm", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert "crm unreachable" in payload["message"]

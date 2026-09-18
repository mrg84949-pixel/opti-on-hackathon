from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Organization, OrganizationService
from web.admin_auth import AdminAuth, get_admin_auth


class _FakeSession:
    def __init__(self, *, org: Organization | None, services: list[OrganizationService] | None = None):
        self._org = org
        self._services = list(services or [])
        self._next_id = 100
        self.committed = False

    async def get(self, model, key):
        if model is Organization:
            return self._org if self._org is not None and self._org.id == key else None
        return None

    async def execute(self, stmt):
        return _FakeResult(self._services)

    async def delete(self, row):
        if row in self._services:
            self._services.remove(row)

    async def flush(self):
        for row in self._services:
            if row.id is None:
                row.id = self._next_id
                self._next_id += 1

    def add(self, row):
        self._services.append(row)

    async def commit(self):
        self.committed = True

    async def refresh(self, row):
        if row.id is None:
            row.id = self._next_id
            self._next_id += 1


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSessionManager:
    def __init__(self, session: _FakeSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _org(**kwargs) -> Organization:
    org = Organization(id=1, name="Clinic A")
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
async def test_get_org_services_returns_items(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    services = [
        OrganizationService(id=1, org_id=1, name="Консультация", price_label="5 000", sort_order=0, is_active=True),
        OrganizationService(id=2, org_id=1, name="УЗИ", price_label="12 000", sort_order=1, is_active=False),
    ]
    session = _FakeSession(org=org, services=services)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/org-services", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["org_id"] == 1
    assert len(payload["items"]) == 2
    assert payload["items"][0]["name"] == "Консультация"
    assert payload["items"][1]["is_active"] is False
    assert payload["catalog_source"] == "org"
    assert payload["services_configured"] is True
    assert "Консультация: 5 000" in payload["catalog_effective"]


@pytest.mark.asyncio
async def test_get_org_services_empty_catalog_meta(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org, services=[])
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/org-services", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["catalog_source"] == "empty"
    assert payload["services_configured"] is False
    assert "не настроен" in payload["catalog_effective"].lower() or "настроен" in payload["catalog_effective"].lower()


@pytest.mark.asyncio
async def test_put_org_services_replace_all(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(
        org=org,
        services=[OrganizationService(id=1, org_id=1, name="Old", sort_order=0)],
    )
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/api/web/org-services",
            headers=_auth_headers(),
            json={
                "items": [
                    {"name": "Новая услуга", "price_label": "3 000", "sort_order": 0},
                    {"name": "Вторая", "price_label": "от 7 000", "sort_order": 1, "is_active": True},
                ]
            },
        )

    assert response.status_code == 200
    assert session.committed is True
    payload = response.json()
    names = [item["name"] for item in payload["items"]]
    assert names == ["Новая услуга", "Вторая"]
    assert all("Old" not in name for name in names)


@pytest.mark.asyncio
async def test_put_org_services_persists_care_message(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/api/web/org-services",
            headers=_auth_headers(),
            json={
                "items": [
                    {
                        "name": "Отбеливание",
                        "price_label": "15 000",
                        "care_message": "Избегайте красящих продуктов 24 часа.",
                        "sort_order": 0,
                    },
                ]
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["care_message"] == "Избегайте красящих продуктов 24 часа."
    assert payload["catalog_source"] == "org"
    assert payload["services_configured"] is True


@pytest.mark.asyncio
async def test_put_org_services_rejects_empty_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/api/web/org-services",
            headers=_auth_headers(),
            json={"items": [{"name": ""}]},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_session_admin_put_org_services_ignores_foreign_x_org_id(monkeypatch: pytest.MonkeyPatch):
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
            response = await client.put(
                "/api/web/org-services",
                headers={**_auth_headers(org_id=2), "X-Admin-Session": "sess"},
                json={"items": [{"name": "Scoped to org 1", "price_label": "1 000"}]},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["org_id"] == 1
    assert payload["items"][0]["name"] == "Scoped to org 1"

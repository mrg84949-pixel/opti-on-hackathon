from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Organization
from web.admin_auth import AdminAuth, get_admin_auth


class _ScalarsAllResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


class _FakeSession:
    def __init__(self, *, execute_results=None, get_results=None):
        self._execute_results = list(execute_results or [])
        self._get_results = dict(get_results or {})
        self.get_calls: list[tuple] = []
        self.execute_calls = 0
        self.committed = False
        self.refreshed = False

    async def execute(self, _stmt):
        self.execute_calls += 1
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call in fake session")
        return self._execute_results.pop(0)

    async def get(self, model, key):
        self.get_calls.append((model, key))
        return self._get_results.get((model, key))

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


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": "1"}


def _org(**overrides):
    base = {
        "id": 1,
        "name": "Org One",
        "billing_paid_until": datetime(2030, 1, 1, tzinfo=timezone.utc),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_list_organizations_session_returns_own_org_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org1 = _org(id=1, name="Clinic A")
    org2 = _org(id=2, name="Clinic B")
    fake_session = _FakeSession(
        get_results={
            (Organization, 1): org1,
            (Organization, 2): org2,
        }
    )

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=2, admin_id=10)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/web/organizations",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == 2
    assert items[0]["name"] == "Clinic B"
    assert fake_session.execute_calls == 0
    assert fake_session.get_calls == [(Organization, 2)]


@pytest.mark.asyncio
async def test_list_organizations_session_unknown_org_empty(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession(get_results={})

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=99, admin_id=10)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/web/organizations",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["items"] == []
    assert fake_session.execute_calls == 0


@pytest.mark.asyncio
async def test_list_organizations_token_returns_all(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org1 = _org(id=1, name="Clinic A")
    org2 = _org(id=2, name="Clinic B")
    fake_session = _FakeSession(execute_results=[_ScalarsAllResult([org1, org2])])

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="token", org_id=None, admin_id=None)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/web/organizations", headers=_auth_headers())
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert [item["id"] for item in items] == [1, 2]
    assert fake_session.execute_calls == 1
    assert fake_session.get_calls == []


@pytest.mark.asyncio
async def test_list_organizations_session_never_returns_other_org(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org1 = _org(id=1, name="Clinic A")
    org2 = _org(id=2, name="Clinic B")
    fake_session = _FakeSession(
        get_results={
            (Organization, 1): org1,
            (Organization, 2): org2,
        }
    )

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=10)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/web/organizations",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    ids = [item["id"] for item in response.json()["items"]]
    assert ids == [1]
    assert 2 not in ids


@pytest.mark.asyncio
async def test_update_billing_ops_token_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    billing_org = _org(id=2, name="Billing")
    fake_session = _FakeSession(get_results={(Organization, 2): billing_org})

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="token", org_id=None, admin_id=None)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                "/api/web/organizations/2/billing",
                headers=_auth_headers(),
                json={"billing_paid_until": "2030-01-01T00:00:00+00:00"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["org_id"] == 2
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_update_billing_session_forbidden(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    billing_org = _org(id=1, name="Clinic A")
    fake_session = _FakeSession(get_results={(Organization, 1): billing_org})

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=10)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                "/api/web/organizations/1/billing",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
                json={"billing_paid_until": "2030-06-01T00:00:00+00:00"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"] == "Ops token required"
    assert fake_session.committed is False
    assert fake_session.get_calls == []


@pytest.mark.asyncio
async def test_update_billing_session_cannot_change_other_org(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession(get_results={(Organization, 2): _org(id=2, name="Other")})

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=10)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                "/api/web/organizations/2/billing",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
                json={"billing_paid_until": "2030-06-01T00:00:00+00:00"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()["detail"] == "Ops token required"
    assert fake_session.committed is False
    assert fake_session.get_calls == []

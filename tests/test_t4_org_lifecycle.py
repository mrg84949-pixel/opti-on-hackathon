from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Admin, Organization
from web import admin_auth
from web.admin_auth import AdminAuth, AmbiguousAdminLoginError, get_admin_auth


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsAllResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


class _LifecycleFakeSession:
    def __init__(self, *, execute_results=None, next_org_id: int = 10, next_admin_id: int = 100):
        self._execute_results = list(execute_results or [])
        self.added: list[object] = []
        self.committed = False
        self._next_org_id = next_org_id
        self._next_admin_id = next_admin_id

    async def execute(self, _stmt):
        if self._execute_results:
            return self._execute_results.pop(0)
        return _ScalarOneOrNoneResult(None)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, Organization) and getattr(obj, "id", None) is None:
                obj.id = self._next_org_id
                self._next_org_id += 1
            if isinstance(obj, Admin) and getattr(obj, "id", None) is None:
                obj.id = self._next_admin_id
                self._next_admin_id += 1

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, obj: object) -> None:
        if isinstance(obj, Organization) and getattr(obj, "id", None) is None:
            obj.id = self._next_org_id
        if isinstance(obj, Admin) and getattr(obj, "id", None) is None:
            obj.id = self._next_admin_id


class _FakeSessionManager:
    def __init__(self, session: _LifecycleFakeSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _token_headers() -> dict[str, str]:
    return {"Authorization": "Bearer 1234"}


@pytest.mark.asyncio
async def test_create_admin_same_login_different_orgs_ok(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _LifecycleFakeSession(execute_results=[_ScalarOneOrNoneResult(None)])

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=2, admin_id=1)

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/web/admins",
                headers={**_token_headers(), "X-Admin-Session": "sess"},
                json={"login": "owner", "password": "secret12"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    assert fake_session.committed is True
    assert len(fake_session.added) == 1
    assert fake_session.added[0].org_id == 2
    assert fake_session.added[0].login == "owner"


@pytest.mark.asyncio
async def test_create_admin_duplicate_login_same_org_409(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    existing = Admin(id=5, login="owner", password_hash="x", org_id=1)
    fake_session = _LifecycleFakeSession(execute_results=[_ScalarOneOrNoneResult(existing)])

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
            response = await client.post(
                "/api/web/admins",
                headers={**_token_headers(), "X-Admin-Session": "sess"},
                json={"login": "owner", "password": "secret12"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_login_without_org_id_single_match(monkeypatch: pytest.MonkeyPatch):
    admin = Admin(id=1, login="owner", password_hash="sha256$x", org_id=1)
    fake_session = _LifecycleFakeSession(
        execute_results=[_ScalarsAllResult([admin])],
    )

    class _DbManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(admin_auth, "AsyncSessionLocal", lambda: _DbManager())
    monkeypatch.setattr(admin_auth, "verify_password", lambda _p, _h: True)
    result = await admin_auth.authenticate_admin_login("owner", "ok")
    assert result is not None
    assert result.org_id == 1


@pytest.mark.asyncio
async def test_login_ambiguous_raises_error(monkeypatch: pytest.MonkeyPatch):
    admins = [
        Admin(id=1, login="owner", password_hash="a", org_id=1),
        Admin(id=2, login="owner", password_hash="b", org_id=2),
    ]
    fake_session = _LifecycleFakeSession(execute_results=[_ScalarsAllResult(admins)])

    class _DbManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(admin_auth, "AsyncSessionLocal", lambda: _DbManager())
    monkeypatch.setattr(admin_auth, "verify_password", lambda _p, _h: True)

    with pytest.raises(AmbiguousAdminLoginError):
        await admin_auth.authenticate_admin_login("owner", "ok")


@pytest.mark.asyncio
async def test_login_ambiguous_requires_org_id_409(monkeypatch: pytest.MonkeyPatch):
    async def fake_authenticate(_login, _password, *, org_id=None):
        raise AmbiguousAdminLoginError()

    monkeypatch.setattr(admin_api, "authenticate_admin_login", fake_authenticate)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/admin/auth/login",
            json={"login": "owner", "password": "ok"},
        )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_login_with_org_id_success(monkeypatch: pytest.MonkeyPatch):
    admin = Admin(id=2, login="owner", password_hash="b", org_id=2)
    fake_session = _LifecycleFakeSession(execute_results=[_ScalarOneOrNoneResult(admin)])

    class _DbManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(admin_auth, "AsyncSessionLocal", lambda: _DbManager())
    monkeypatch.setattr(admin_auth, "verify_password", lambda _p, _h: True)
    result = await admin_auth.authenticate_admin_login("owner", "ok", org_id=2)
    assert result is not None
    assert result.id == 2


@pytest.mark.asyncio
async def test_post_organizations_ops_token_201(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _LifecycleFakeSession()

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
            response = await client.post(
                "/api/web/organizations",
                headers=_token_headers(),
                json={
                    "name": "New Clinic",
                    "initial_admin": {"login": "owner", "password": "secret12"},
                },
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    payload = response.json()
    assert payload["name"] == "New Clinic"
    assert payload["id"] == 10
    assert payload["initial_admin"]["login"] == "owner"
    assert fake_session.committed is True
    assert len(fake_session.added) == 2


@pytest.mark.asyncio
async def test_post_organizations_session_403(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=1)

    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/web/organizations",
                headers={**_token_headers(), "X-Admin-Session": "sess"},
                json={"name": "Blocked"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_post_organizations_no_token_401(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/organizations",
            json={"name": "No auth"},
        )
    assert response.status_code == 401

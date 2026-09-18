from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Organization
from web.admin_auth import AdminAuth, get_admin_auth


class _FakeSession:
    def __init__(self):
        self.committed = False

    async def get(self, model, key):
        if model is Organization:
            return SimpleNamespace(id=key, billing_paid_until=None, bot_enabled=True)
        return None

    async def commit(self):
        self.committed = True


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers(*, org_id: int = 1) -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": str(org_id)}


@pytest.mark.asyncio
async def test_session_cannot_read_other_org_customer_conversation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    seen: dict[str, int] = {}

    async def fake_get_conversation(session, customer_id, org_id, *, limit=100):
        seen["org_id"] = org_id
        return None

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=10)

    monkeypatch.setattr(admin_api.customer_service, "get_conversation", fake_get_conversation)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession()))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/web/customers/99/conversation?org_id=2",
                headers={**_auth_headers(org_id=2), "X-Admin-Session": "sess"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert seen["org_id"] == 1


@pytest.mark.asyncio
async def test_session_cannot_confirm_other_org_appointment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    seen: dict[str, int] = {}

    async def fake_confirm(session, org_id, appt_id):
        seen["org_id"] = org_id
        raise admin_api.appointment_service.AppointmentNotFoundError("not found")

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=10)

    monkeypatch.setattr(admin_api.appointment_service, "confirm_appointment", fake_confirm)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession()))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/web/appointments/42/confirm",
                headers={**_auth_headers(org_id=2), "X-Admin-Session": "sess"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert seen["org_id"] == 1


@pytest.mark.asyncio
async def test_bearer_wrong_org_header_customer_profile_404(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")

    async def fake_get_profile(session, customer_id, org_id):
        assert org_id == 1
        return None

    monkeypatch.setattr(admin_api.customer_service, "get_customer_profile", fake_get_profile)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession()))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/web/customers/5/profile?org_id=2",
            headers=_auth_headers(org_id=1),
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_session_mute_uses_auth_org_not_body(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    seen: dict[str, int] = {}

    async def fake_set_mute(session, customer_id, org_id, *, days=None, muted_until=None):
        seen["org_id"] = org_id
        return None

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=10)

    monkeypatch.setattr(admin_api.customer_service, "set_mute", fake_set_mute)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession()))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.put(
                "/api/web/customers/5/mute",
                headers={"X-Admin-Session": "sess", "Authorization": "Bearer 1234"},
                json={"org_id": 2, "days": 7},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert seen["org_id"] == 1


@pytest.mark.asyncio
async def test_session_erase_uses_auth_org_not_query(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    seen: dict[str, int] = {}

    async def fake_erase(session, customer_id, org_id):
        seen["org_id"] = org_id
        seen["customer_id"] = customer_id
        return None

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=10)

    monkeypatch.setattr(admin_api.customer_service, "erase_customer_for_org", fake_erase)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession()))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.request(
                "DELETE",
                "/api/web/customers/5",
                headers={"X-Admin-Session": "sess", "Authorization": "Bearer 1234"},
                json={"confirm": True},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert seen["org_id"] == 1
    assert seen["customer_id"] == 5


@pytest.mark.asyncio
async def test_session_crm_appointment_sync_uses_auth_org_not_header(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    seen: dict[str, int] = {}

    async def fake_sync(session, org_id):
        seen["org_id"] = org_id
        return {"org_id": org_id, "skipped": False, "synced": 0, "errors": 0}

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=10)

    monkeypatch.setattr(
        admin_api.crm_appointment_sync_service,
        "sync_org_crm_cancellations",
        fake_sync,
    )
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession()))
    app = _test_app()
    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/web/crm/appointments/sync",
                headers={**_auth_headers(org_id=2), "X-Admin-Session": "sess"},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert seen["org_id"] == 1

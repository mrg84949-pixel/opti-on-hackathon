from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.services import customer_service


class _FakeSession:
    def __init__(self):
        self.committed = False
        self.deleted: list = []

    async def commit(self):
        self.committed = True


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _test_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers(*, org_id: int = 1) -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": str(org_id)}


@pytest.mark.asyncio
async def test_erase_customer_for_org_deletes_logs_and_customer(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=5, phone="tg:12345", org_id=1)
    execute_calls = 0

    class Session:
        deleted: list = []

        async def execute(self, _stmt):
            nonlocal execute_calls
            execute_calls += 1
            if execute_calls == 1:
                return SimpleNamespace(rowcount=2)
            return SimpleNamespace(scalar_one=lambda: 3)

        async def delete(self, obj):
            Session.deleted.append(obj)

        async def flush(self):
            return None

    async def fake_get(_session, customer_id, org_id):
        assert customer_id == 5
        assert org_id == 1
        return customer if customer_id == 5 else None

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)
    session = Session()
    result = await customer_service.erase_customer_for_org(session, 5, 1)

    assert result is not None
    assert result["customer_id"] == 5
    assert result["org_id"] == 1
    assert result["logs_removed"] == 2
    assert result["appointments_removed"] == 3
    assert Session.deleted == [customer]
    assert "phone" not in result
    assert "name" not in result


@pytest.mark.asyncio
async def test_erase_customer_for_org_missing_returns_none(monkeypatch: pytest.MonkeyPatch):
    class Session:
        async def execute(self, _stmt):
            raise AssertionError("should not execute")

    async def fake_get(_session, _customer_id, _org_id):
        return None

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)
    result = await customer_service.erase_customer_for_org(Session(), 99, 1)

    assert result is None


@pytest.mark.asyncio
async def test_erase_customer_route_happy_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession()

    async def fake_erase(session, customer_id, org_id):
        assert customer_id == 5
        assert org_id == 1
        return {
            "customer_id": 5,
            "org_id": 1,
            "logs_removed": 4,
            "appointments_removed": 2,
        }

    monkeypatch.setattr(admin_api.customer_service, "erase_customer_for_org", fake_erase)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            "DELETE",
            "/api/web/customers/5",
            headers=_auth_headers(),
            json={"confirm": True},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["logs_removed"] == 4
    assert payload["appointments_removed"] == 2
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_erase_customer_route_requires_confirm(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            "DELETE",
            "/api/web/customers/5",
            headers=_auth_headers(),
            json={"confirm": False},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_erase_customer_route_not_found(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")

    async def fake_erase(session, customer_id, org_id):
        return None

    monkeypatch.setattr(admin_api.customer_service, "erase_customer_for_org", fake_erase)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession()))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.request(
            "DELETE",
            "/api/web/customers/99",
            headers=_auth_headers(),
            json={"confirm": True},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_erase_customer_route_repeat_not_found(monkeypatch: pytest.MonkeyPatch):
    """Second DELETE after erase returns 404."""
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    calls = 0

    async def fake_erase(session, customer_id, org_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"customer_id": 5, "org_id": 1, "logs_removed": 1, "appointments_removed": 0}
        return None

    monkeypatch.setattr(admin_api.customer_service, "erase_customer_for_org", fake_erase)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession()))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.request(
            "DELETE",
            "/api/web/customers/5",
            headers=_auth_headers(),
            json={"confirm": True},
        )
        second = await client.request(
            "DELETE",
            "/api/web/customers/5",
            headers=_auth_headers(),
            json={"confirm": True},
        )
    assert first.status_code == 200
    assert second.status_code == 404

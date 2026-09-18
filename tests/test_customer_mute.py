from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.services import customer_service


def test_phone_to_external_user_id():
    assert customer_service.phone_to_external_user_id("tg:12345") == "12345"
    assert customer_service.phone_to_external_user_id("wa:7999") == "7999"


def test_is_customer_muted():
    now = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    c = SimpleNamespace(muted_until=now + timedelta(days=1))
    assert customer_service.is_customer_muted(c, now=now) is True
    c.muted_until = now - timedelta(days=1)
    assert customer_service.is_customer_muted(c, now=now) is False


def test_logs_to_messages():
    log = SimpleNamespace(
        id=7,
        user_message_preview="Hi",
        reply_preview="Hello",
        status="ok",
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    msgs = customer_service._logs_to_messages([log])
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"


class _FakeSession:
    def __init__(self):
        self.committed = False

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


def _auth_headers():
    return {"Authorization": "Bearer 1234", "x-org-id": "1"}


@pytest.mark.asyncio
async def test_mute_customer_route(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession()

    async def fake_set_mute(session, customer_id, org_id, *, days=None, muted_until=None):
        assert customer_id == 5
        assert org_id == 1
        assert days == 7
        return {
            "customer_id": 5,
            "muted_until": "2026-06-01T00:00:00+00:00",
            "no_show_count": 2,
        }

    monkeypatch.setattr(admin_api.customer_service, "set_mute", fake_set_mute)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/api/web/customers/5/mute",
            headers=_auth_headers(),
            json={"org_id": 1, "days": 7},
        )
    assert response.status_code == 200
    assert response.json()["no_show_count"] == 2
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_unmute_customer_route(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession()

    async def fake_clear(session, customer_id, org_id):
        return {"customer_id": customer_id, "muted_until": None, "no_show_count": 0}

    monkeypatch.setattr(admin_api.customer_service, "clear_mute", fake_clear)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(
            "/api/web/customers/3/mute",
            headers=_auth_headers(),
        )
    assert response.status_code == 200
    assert response.json()["muted_until"] is None


@pytest.mark.asyncio
async def test_conversation_route(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession()

    async def fake_get(session, customer_id, org_id, *, limit=100):
        return {
            "customer_id": customer_id,
            "org_id": org_id,
            "channel": "telegram",
            "external_user_id": "42",
            "muted_until": None,
            "no_show_count": 0,
            "messages": [{"id": "m1", "role": "user", "text": "test", "at": "t", "status": "ok"}],
        }

    monkeypatch.setattr(admin_api.customer_service, "get_conversation", fake_get)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/web/customers/2/conversation",
            headers=_auth_headers(),
        )
    assert response.status_code == 200
    assert response.json()["messages"][0]["role"] == "user"

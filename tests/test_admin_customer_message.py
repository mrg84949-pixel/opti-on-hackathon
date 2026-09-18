from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.services import customer_service
from bot.services.outbound_result import OutboundSendResult


def test_logs_to_messages_admin_send():
    log = SimpleNamespace(
        id=9,
        user_message_preview="",
        reply_preview="Admin outreach",
        status=customer_service.ADMIN_SEND_STATUS,
        created_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    msgs = customer_service._logs_to_messages([log])
    assert len(msgs) == 1
    assert msgs[0]["role"] == "admin"
    assert msgs[0]["text"] == "Admin outreach"


class _FakeSession:
    def __init__(self, org):
        self.org = org
        self.committed = False
        self.added: list = []

    async def get(self, _model, org_id):
        return self.org if self.org and self.org.id == org_id else None

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
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


def _test_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers():
    return {"Authorization": "Bearer 1234", "x-org-id": "1"}


@pytest.mark.asyncio
async def test_send_admin_message_service_happy_path(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=5, phone="tg:12345")
    org = SimpleNamespace(id=1)
    session = _FakeSession(org)

    async def fake_get(session_arg, customer_id, org_id):
        assert customer_id == 5
        assert org_id == 1
        return customer

    async def fake_send(org_arg, customer_arg, text):
        assert org_arg is org
        assert customer_arg is customer
        assert text == "Hello from admin"
        return OutboundSendResult.success(channel="telegram")

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)
    monkeypatch.setattr(customer_service.notification_service, "send_customer_message", fake_send)

    result = await customer_service.send_admin_message(
        session, 5, 1, text="Hello from admin"
    )
    assert result is not None
    assert result["notification_sent"] is True
    assert result["customer_id"] == 5
    assert len(session.added) == 1
    log = session.added[0]
    assert log.status == customer_service.ADMIN_SEND_STATUS
    assert log.reply_preview == "Hello from admin"


@pytest.mark.asyncio
async def test_send_admin_message_unsupported_channel(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=5, phone="+77001234567")
    session = _FakeSession(SimpleNamespace(id=1))

    async def fake_get(session_arg, customer_id, org_id):
        return customer

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)

    result = await customer_service.send_admin_message(session, 5, 1, text="Hi")
    assert result is not None
    assert result["error_code"] == "unsupported_channel"
    assert session.added == []


@pytest.mark.asyncio
async def test_send_admin_message_skipped_send(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=5, phone="wa:7999")
    org = SimpleNamespace(id=1)
    session = _FakeSession(org)

    async def fake_get(session_arg, customer_id, org_id):
        return customer

    async def fake_send(org_arg, customer_arg, text):
        return OutboundSendResult.skipped(channel="whatsapp")

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)
    monkeypatch.setattr(customer_service.notification_service, "send_customer_message", fake_send)

    result = await customer_service.send_admin_message(session, 5, 1, text="Retry later")
    assert result is not None
    assert result["notification_sent"] is False
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_send_customer_message_route_happy_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession(SimpleNamespace(id=1))

    async def fake_send_admin(session, customer_id, org_id, *, text):
        assert customer_id == 5
        assert org_id == 1
        assert text == "Manual note"
        return {
            "customer_id": 5,
            "message_preview": "Manual note",
            "notification_sent": True,
            "auth_failed": False,
            "channel": "telegram",
        }

    monkeypatch.setattr(admin_api.customer_service, "send_admin_message", fake_send_admin)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/customers/5/message",
            headers=_auth_headers(),
            json={"message": "Manual note"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["notification_sent"] is True
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_send_customer_message_route_cross_org_404(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession(SimpleNamespace(id=1))

    async def fake_send_admin(session, customer_id, org_id, *, text):
        return None

    monkeypatch.setattr(admin_api.customer_service, "send_admin_message", fake_send_admin)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/customers/99/message",
            headers=_auth_headers(),
            json={"message": "Hi"},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_send_customer_message_route_validation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        empty = await client.post(
            "/api/web/customers/5/message",
            headers=_auth_headers(),
            json={"message": ""},
        )
        too_long = await client.post(
            "/api/web/customers/5/message",
            headers=_auth_headers(),
            json={"message": "x" * 2001},
        )
    assert empty.status_code == 422
    assert too_long.status_code == 422


@pytest.mark.asyncio
async def test_send_customer_message_route_unsupported_channel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession(SimpleNamespace(id=1))

    async def fake_send_admin(session, customer_id, org_id, *, text):
        return {
            "error_code": "unsupported_channel",
            "customer_id": customer_id,
            "channel": "phone",
        }

    monkeypatch.setattr(admin_api.customer_service, "send_admin_message", fake_send_admin)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/customers/5/message",
            headers=_auth_headers(),
            json={"message": "Hi"},
        )
    assert response.status_code == 422

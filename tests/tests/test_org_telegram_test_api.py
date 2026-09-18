from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Organization
from bot.services import telegram_org_service as tg_service


class _FakeSession:
    def __init__(self, *, org: Organization | None):
        self._org = org

    async def get(self, model, key):
        if model is Organization:
            return self._org if self._org is not None and self._org.id == key else None
        return None


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
async def test_test_telegram_no_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(telegram_bot_token=None)
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    async def _no_token(_org):
        return tg_service.TelegramConnectionTestResult(
            ok=False,
            token_set=False,
            bot_username=None,
            webhook_url=None,
            expected_webhook_url="https://api.example/bot/webhook",
            message="Telegram bot token is not configured for this organization.",
        )

    monkeypatch.setattr(admin_api, "test_telegram_connection", _no_token)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/org-integrations/test-telegram", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["token_set"] is False


@pytest.mark.asyncio
async def test_test_telegram_getme_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    async def _success(_org):
        return tg_service.TelegramConnectionTestResult(
            ok=True,
            token_set=True,
            bot_username="@clinic_bot",
            webhook_url="https://api.example/bot/webhook",
            expected_webhook_url="https://api.example/bot/webhook",
            message="Подключение успешно: @clinic_bot, webhook настроен.",
        )

    monkeypatch.setattr(admin_api, "test_telegram_connection", _success)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/org-integrations/test-telegram", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["bot_username"] == "@clinic_bot"
    assert payload["webhook_url"] == "https://api.example/bot/webhook"


@pytest.mark.asyncio
async def test_test_telegram_invalid_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    async def _invalid(_org):
        return tg_service.TelegramConnectionTestResult(
            ok=False,
            token_set=True,
            bot_username=None,
            webhook_url=None,
            expected_webhook_url=None,
            message="Unauthorized",
        )

    monkeypatch.setattr(admin_api, "test_telegram_connection", _invalid)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/org-integrations/test-telegram", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["token_set"] is True
    assert "Unauthorized" in payload["message"]


@pytest.mark.asyncio
async def test_register_telegram_webhook_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    async def _register(_org):
        return tg_service.TelegramConnectionTestResult(
            ok=True,
            token_set=True,
            bot_username="@clinic_bot",
            webhook_url="https://api.example/bot/webhook",
            expected_webhook_url="https://api.example/bot/webhook",
            message="Webhook зарегистрирован: @clinic_bot.",
        )

    monkeypatch.setattr(admin_api, "register_telegram_webhook", _register)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/org-integrations/register-telegram-webhook",
            headers=_auth_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["webhook_url"] == "https://api.example/bot/webhook"


@pytest.mark.asyncio
async def test_get_org_integrations_send_unhealthy_when_auth_broken(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org()
    session = _FakeSession(org=org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))
    from bot.services.outbound_health import record_auth_failure, reset_outbound_health_cache

    reset_outbound_health_cache()
    record_auth_failure(1, "telegram")

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/org-integrations", headers=_auth_headers())

    reset_outbound_health_cache()

    assert response.status_code == 200
    assert response.json()["telegram_send_healthy"] is False

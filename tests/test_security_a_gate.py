"""Wave 2 Track A exit gate — thin A.1–A.4 checks (mocked)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import bot.api.whatsapp as whatsapp_api
import web.admin_api as admin_api
from bot.db.models import Organization
from bot.services import telegram_org_service as tg_org
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


class _GreenResolveScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _GreenResolveSession:
    def __init__(self, org):
        self.org = org

    async def execute(self, stmt):
        crit = stmt.whereclause
        field = crit.left.key
        value = crit.right.value
        if field == "whatsapp_instance_id" and getattr(self.org, field, None) == value:
            return _GreenResolveScalarResult(self.org)
        return _GreenResolveScalarResult(None)


class _OrgIntegrationsSession:
    def __init__(self, org: Organization):
        self._org = org

    async def get(self, model, key):
        if model is Organization and self._org.id == key:
            return self._org
        return None


def _admin_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _wa_app() -> FastAPI:
    app = FastAPI()
    app.include_router(whatsapp_api.router, prefix="/bot")
    return app


def _auth_headers(*, org_id: int = 1) -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": str(org_id)}


@pytest.mark.asyncio
async def test_a1_confirm_rejects_cross_org_session_scope(monkeypatch: pytest.MonkeyPatch):
    """A.1: session org=1 cannot confirm with x-org-id=2 → 404, scoped to session org."""
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
    app = _admin_app()
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
async def test_a2_strict_outbound_skips_env_telegram(monkeypatch: pytest.MonkeyPatch):
    """A.2: TENANT_CONFIG_STRICT — org without token must not send via env fallback."""
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.telegram_token", "env-tg-token")
    called = False

    class _ShouldNotRun:
        async def __aenter__(self):
            nonlocal called
            called = True
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("bot.services.telegram_org_service.httpx.AsyncClient", lambda timeout=20: _ShouldNotRun())
    org = SimpleNamespace(id=1, telegram_bot_token=None)
    ok = await tg_org.send_telegram_for_org(org, 777, "hello")
    assert ok.ok is False
    assert called is False


@pytest.mark.asyncio
async def test_a3_wa_unknown_instance_org_not_found(monkeypatch: pytest.MonkeyPatch):
    """A.3: unknown Green instanceId → org_not_found, no LLM."""
    demo_org = SimpleNamespace(
        id=1,
        whatsapp_instance_id="42",
        whatsapp_provider="green",
        whatsapp_api_token="t",
    )
    llm_calls: list[dict] = []

    async def fake_llm(**kwargs):
        llm_calls.append(kwargs)
        return "should-not-run"

    monkeypatch.setattr(
        whatsapp_api,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(_GreenResolveSession(demo_org)),
    )
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)

    app = _wa_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/bot/whatsapp/webhook",
            json={
                "instanceId": "99",
                "senderData": {"chatId": "777@c.us"},
                "messageData": {"textMessageData": {"textMessage": "hello"}},
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["detail"] == "org_not_found"
    assert llm_calls == []


@pytest.mark.asyncio
async def test_a4_org_integrations_get_masks_secrets(monkeypatch: pytest.MonkeyPatch):
    """A.4: GET org-integrations returns *_set flags only, no raw secrets."""
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(
        id=1,
        name="Clinic A",
        crm_provider="amocrm",
        crm_base_url="https://crm.example",
        crm_api_token="secret-crm-token",
        telegram_bot_token="secret-tg-token",
    )
    session = _OrgIntegrationsSession(org)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(session))

    app = _admin_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/org-integrations", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["crm_api_token_set"] is True
    assert payload["telegram_bot_token_set"] is True
    assert "crm_api_token" not in payload
    assert "telegram_bot_token" not in payload
    assert "secret-crm-token" not in response.text
    assert "secret-tg-token" not in response.text

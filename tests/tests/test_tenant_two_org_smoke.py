from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import bot.api.whatsapp as whatsapp_api
import web.admin_api as admin_api
from bot.channels.whatsapp import outbound
from bot.db.models import Organization
from bot.services import notification_service
from bot.services import telegram_org_service as tg_org
from bot.services.org_secrets import get_org_secret
from bot.services.secret_encryption import hash_secret
from web.admin_auth import AdminAuth, get_admin_auth

ORG_A_TG = "111:TOKEN-A"
ORG_B_TG = "222:TOKEN-B"
ENV_TG = "999:ENV-TOKEN"


def _org_a(**overrides):
    base = {
        "id": 1,
        "telegram_bot_token": ORG_A_TG,
        "telegram_bot_token_hash": hash_secret(ORG_A_TG),
        "whatsapp_provider": "green",
        "whatsapp_instance_id": "inst-a",
        "whatsapp_api_token": "wa-token-a",
        "whatsapp_meta_phone_number_id": None,
        "whatsapp_meta_access_token": None,
        "whatsapp_meta_reminder_template_lang": "ru",
        "crm_base_url": "https://a.example",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _org_b(**overrides):
    base = {
        "id": 2,
        "telegram_bot_token": ORG_B_TG,
        "telegram_bot_token_hash": hash_secret(ORG_B_TG),
        "whatsapp_provider": "meta",
        "whatsapp_instance_id": None,
        "whatsapp_api_token": None,
        "whatsapp_meta_phone_number_id": "pn-b",
        "whatsapp_meta_access_token": "meta-token-b",
        "whatsapp_meta_reminder_template_lang": "ru",
        "crm_base_url": "https://b.example",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _ScalarsAllResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


class _TgResolveFakeSession:
    def __init__(self, orgs: list):
        self.orgs = orgs

    async def execute(self, stmt):
        crit = stmt.whereclause
        if crit is not None and getattr(crit.left, "key", None) == "telegram_bot_token_hash":
            token_hash = crit.right.value
            matched = [o for o in self.orgs if getattr(o, "telegram_bot_token_hash", None) == token_hash]
            return _ScalarsAllResult(matched)
        matched = [o for o in self.orgs if getattr(o, "telegram_bot_token", None)]
        return _ScalarsAllResult(matched)


class _TgResolveSessionManager:
    def __init__(self, orgs: list):
        self.orgs = orgs

    async def __aenter__(self):
        return _TgResolveFakeSession(self.orgs)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


def _match_org_from_stmt(stmt, orgs: list):
    crit = stmt.whereclause
    if crit is None:
        return None
    field = crit.left.key
    value = crit.right.value
    for org in orgs:
        if getattr(org, field, None) == value:
            return org
    return None


class _WaResolveFakeSession:
    def __init__(self, orgs: list):
        self.orgs = orgs

    async def execute(self, stmt):
        return _ScalarOneOrNoneResult(_match_org_from_stmt(stmt, self.orgs))

    async def get(self, _model, org_id: int):
        for org in self.orgs:
            if org.id == org_id:
                return org
        return None


class _WaResolveSessionManager:
    def __init__(self, orgs: list):
        self.orgs = orgs

    async def __aenter__(self):
        return _WaResolveFakeSession(self.orgs)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeHttpClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200, is_success=True)


@pytest.fixture
def strict_env_noise(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.telegram_token", ENV_TG)
    monkeypatch.setattr("bot.config.settings.green_api_instance_id", "env-inst")
    monkeypatch.setattr("bot.config.settings.green_api_token", "env-green")
    monkeypatch.setattr("bot.config.settings.whatsapp_phone_number_id", "env-pn")
    monkeypatch.setattr("bot.config.settings.whatsapp_graph_access_token", "env-meta")


@pytest.mark.asyncio
async def test_tg_resolve_maps_each_org_token(strict_env_noise, monkeypatch: pytest.MonkeyPatch):
    org_a = _org_a()
    org_b = _org_b()
    monkeypatch.setattr(
        tg_org,
        "AsyncSessionLocal",
        lambda: _TgResolveSessionManager([org_a, org_b]),
    )

    assert (await tg_org.resolve_org_by_telegram_bot_token(ORG_A_TG)) is org_a
    assert (await tg_org.resolve_org_by_telegram_bot_token(ORG_B_TG)) is org_b
    assert await tg_org.resolve_org_by_telegram_bot_token("unknown-token") is None


@pytest.mark.asyncio
async def test_tg_send_uses_org_token_not_env(strict_env_noise, monkeypatch: pytest.MonkeyPatch):
    client = _FakeHttpClient()
    monkeypatch.setattr("bot.services.telegram_org_service.httpx.AsyncClient", lambda timeout=20: client)
    org_a = _org_a()

    ok = await tg_org.send_telegram_for_org(org_a, 1001, "hello")

    assert ok.ok is True
    assert ORG_A_TG in client.calls[0]["url"]
    assert ENV_TG not in client.calls[0]["url"]


@pytest.mark.asyncio
async def test_wa_green_resolve_and_send_org_scoped(strict_env_noise, monkeypatch: pytest.MonkeyPatch):
    org_a = _org_a()
    org_b = _org_b(whatsapp_provider="green", whatsapp_instance_id="inst-b", whatsapp_api_token="wa-token-b")
    monkeypatch.setattr(
        whatsapp_api,
        "AsyncSessionLocal",
        lambda: _WaResolveSessionManager([org_a, org_b]),
    )

    assert (await whatsapp_api._resolve_org_green("inst-a")) is org_a
    assert (await whatsapp_api._resolve_org_green("inst-b")) is org_b

    captured: dict[str, str] = {}

    async def fake_green_send(**kwargs):
        captured.update(kwargs)
        return True, None, False

    monkeypatch.setattr(outbound.green_api, "send_message", fake_green_send)
    ok = await outbound.send_whatsapp_text(org_b, "777@c.us", "hi")

    assert ok.ok is True
    assert captured["instance_id"] == "inst-b"
    assert captured["api_token"] == "wa-token-b"
    assert captured["instance_id"] != "env-inst"


@pytest.mark.asyncio
async def test_wa_meta_resolve_org_scoped(strict_env_noise, monkeypatch: pytest.MonkeyPatch):
    org_a = _org_a()
    org_b = _org_b()
    monkeypatch.setattr(
        whatsapp_api,
        "AsyncSessionLocal",
        lambda: _WaResolveSessionManager([org_a, org_b]),
    )

    assert (await whatsapp_api._resolve_org_meta("pn-b")) is org_b
    assert (await whatsapp_api._resolve_org_meta("pn-unknown")) is None


@pytest.mark.asyncio
async def test_notification_uses_org_b_tg_not_env(strict_env_noise, monkeypatch: pytest.MonkeyPatch):
    client = _FakeHttpClient()
    monkeypatch.setattr("bot.services.telegram_org_service.httpx.AsyncClient", lambda timeout=20: client)
    org_b = _org_b()

    ok = await notification_service.send_customer_message(
        org_b,
        SimpleNamespace(phone="tg:2002"),
        "reminder",
    )

    assert ok.ok is True
    assert ORG_B_TG in client.calls[0]["url"]
    assert ENV_TG not in client.calls[0]["url"]


class _DualOrgFakeSession:
    def __init__(self, orgs: dict[int, Organization]):
        self.orgs = orgs
        self.committed = False

    async def get(self, model, key):
        if model is Organization:
            return self.orgs.get(key)
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        return None


class _DualOrgSessionManager:
    def __init__(self, session: _DualOrgFakeSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers(*, org_id: int = 1) -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": str(org_id)}


@pytest.mark.asyncio
async def test_org_integrations_isolated_per_admin_session(strict_env_noise, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org1 = Organization(id=1, name="Clinic A", crm_base_url="https://a.example")
    org2 = Organization(id=2, name="Clinic B", crm_base_url="https://b.example")
    session = _DualOrgFakeSession({1: org1, 2: org2})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _DualOrgSessionManager(session))

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
    assert org1.crm_base_url == "https://evil.example"
    assert org2.crm_base_url == "https://b.example"


def test_get_org_secret_used_for_tg_resolve_fallback(monkeypatch: pytest.MonkeyPatch):
    org = _org_a()
    assert get_org_secret(org, "telegram_bot_token") == ORG_A_TG

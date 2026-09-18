from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Admin, Organization
from web.admin_auth import AdminAuth, get_admin_auth

FORBIDDEN_RESPONSE_KEYS = frozenset({
    "whatsapp_api_token",
    "whatsapp_meta_access_token",
    "telegram_bot_token",
    "crm_api_token",
    "password_hash",
})

KNOWN_SECRETS = (
    "secret-meta-token-xyz",
    "secret-green-token-abc",
    "secret-tg-token-123",
    "secret-crm-token-456",
)

ADMIN_GET_PATHS = (
    "/api/web/whatsapp-settings",
    "/api/web/org-integrations",
    "/api/web/org-settings",
    "/api/web/organizations",
    "/api/web/admins",
    "/api/web/prompts",
    "/api/web/dashboard/summary",
    "/api/web/crm/staff",
    "/api/web/stats",
    "/api/web/stats/activity",
    "/api/web/interaction-logs",
)


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
        self.committed = False
        self.refreshed = False

    async def execute(self, _stmt):
        if not self._execute_results:
            return _ScalarsAllResult([])
        return self._execute_results.pop(0)

    async def get(self, model, key):
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


def _secret_org() -> Organization:
    org = Organization(
        name="Secret Clinic",
        whatsapp_provider="meta",
        whatsapp_instance_id="inst-1",
        whatsapp_api_token=KNOWN_SECRETS[1],
        whatsapp_meta_phone_number_id="pn-1",
        whatsapp_meta_access_token=KNOWN_SECRETS[0],
        whatsapp_meta_reminder_template_name="reminder",
        whatsapp_meta_reminder_template_lang="ru",
        whatsapp_broadcast_quota_monthly=100,
        whatsapp_broadcast_sent_count=0,
        whatsapp_broadcast_month_key="2026-05",
        telegram_bot_token=KNOWN_SECRETS[2],
        crm_provider="demo",
        crm_base_url="https://crm.example",
        crm_api_token=KNOWN_SECRETS[3],
        system_prompt="Clinic prompt with enough length for tuned check here.",
        bot_enabled=True,
    )
    org.id = 1
    return org


def _collect_keys(obj: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(key)
            keys.update(_collect_keys(value))
    elif isinstance(obj, list):
        for item in obj:
            keys.update(_collect_keys(item))
    return keys


def _assert_no_forbidden_keys(payload: Any) -> None:
    assert not (_collect_keys(payload) & FORBIDDEN_RESPONSE_KEYS)


def _assert_no_known_secrets(text: str) -> None:
    for secret in KNOWN_SECRETS:
        assert secret not in text


def _install_read_mocks(
    monkeypatch: pytest.MonkeyPatch,
    org: Organization,
    *,
    path: str,
) -> _FakeSession:
    admins = [Admin(id=1, login="owner", password_hash="sha256$hidden", org_id=1)]
    execute_results = []
    if path.endswith("/admins"):
        execute_results.append(_ScalarsAllResult(admins))
    fake_session = _FakeSession(
        execute_results=execute_results,
        get_results={
            (Organization, 1): org,
            (Admin, 1): admins[0],
        },
    )

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=1)

    async def fake_stats(_session, org_id):
        return {"total_customers": 0, "upcoming_appointments_30d": 0}

    async def fake_activity(_session, org_id):
        return {"day": [], "week": [], "month": [], "year": []}

    async def fake_dashboard(_session, org_id):
        return {
            "org_id": org_id,
            "org_name": org.name,
            "found": True,
            "pending_appointments": [],
            "pending_count": 0,
            "setup": {
                "whatsapp_connected": True,
                "telegram_connected": True,
                "crm_connected": True,
                "services_configured": True,
                "prompt_tuned": True,
                "bot_enabled": True,
            },
            "impact": {
                "total_customers": 0,
                "upcoming_30d": 0,
                "unique_contacts_30d": 0,
                "unique_contacts_prev_30d": 0,
                "bookings_30d": 0,
                "bookings_prev_30d": 0,
                "completed_30d": 0,
                "bot_dialogs_30d": 0,
            },
        }

    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    monkeypatch.setattr(admin_api.stats_service, "get_business_stats", fake_stats)
    monkeypatch.setattr(admin_api.stats_service, "get_activity_stats", fake_activity)
    monkeypatch.setattr(admin_api.stats_service, "get_dashboard_summary", fake_dashboard)
    return fake_session


@pytest.mark.asyncio
async def test_whatsapp_settings_get_masks_tokens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _secret_org()
    fake_session = _FakeSession(get_results={(Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/whatsapp-settings", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta_access_token_set"] is True
    assert payload["whatsapp_api_token_set"] is True
    _assert_no_forbidden_keys(payload)
    _assert_no_known_secrets(response.text)


@pytest.mark.asyncio
async def test_whatsapp_settings_put_response_masks_tokens(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _secret_org()
    org.whatsapp_api_token = None
    org.whatsapp_meta_access_token = None
    fake_session = _FakeSession(get_results={(Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.put(
            "/api/web/whatsapp-settings",
            headers=_auth_headers(),
            json={
                "whatsapp_api_token": KNOWN_SECRETS[1],
                "whatsapp_meta_access_token": KNOWN_SECRETS[0],
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta_access_token_set"] is True
    assert payload["whatsapp_api_token_set"] is True
    _assert_no_forbidden_keys(payload)
    _assert_no_known_secrets(response.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ADMIN_GET_PATHS)
async def test_admin_get_endpoints_never_return_secret_keys(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _secret_org()
    _install_read_mocks(monkeypatch, org, path=path)
    app = _test_app()
    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=1)

    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                path,
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    _assert_no_forbidden_keys(response.json())


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ADMIN_GET_PATHS)
async def test_admin_get_endpoints_never_contain_known_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _secret_org()
    _install_read_mocks(monkeypatch, org, path=path)
    app = _test_app()

    async def fake_get_admin_auth(
        authorization: str | None = None,
        x_admin_session: str | None = None,
    ) -> AdminAuth:
        return AdminAuth(mode="session", org_id=1, admin_id=1)

    app.dependency_overrides[get_admin_auth] = fake_get_admin_auth
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                path,
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    _assert_no_known_secrets(response.text)

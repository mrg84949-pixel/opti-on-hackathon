from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import Admin, Organization, OrganizationCrmStaffCache
from web.admin_auth import AdminAuth, get_admin_auth
from bot.services import crm_staff_service


class _ScalarOneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _ScalarsAllResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


class _AllRowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, *, execute_results=None, get_results=None):
        self._execute_results = list(execute_results or [])
        self._get_results = dict(get_results or {})
        self.committed = False
        self.refreshed = False
        self.added: list[object] = []

    async def execute(self, _stmt):
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call in test fake session")
        return self._execute_results.pop(0)

    async def get(self, model, key):
        return self._get_results.get((model, key))

    def add(self, obj: object) -> None:
        self.added.append(obj)

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


@pytest.mark.asyncio
async def test_stats_requires_auth(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/stats")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_stats_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    fake_session = _FakeSession(
        execute_results=[
            _ScalarOneResult(10),  # total_customers
            _ScalarOneResult(5),  # upcoming
            _ScalarOneResult(2),  # new_30d
            _ScalarOneResult(1),  # cancelled_30d
            _ScalarOneResult(3),  # completed_30d
            _ScalarOneResult(4),  # confirmed_30d
            _ScalarOneResult(750_000),  # completed_revenue_30d_minor
            _ScalarOneResult(1),  # completed_revenue_unpriced_count
        ]
    )
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/stats", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["total_customers"] == 10
    assert payload["upcoming_appointments_30d"] == 5
    assert payload["new_30d"] == 2
    assert payload["cancelled_30d"] == 1
    assert payload["completed_30d"] == 3
    assert payload["confirmed_30d"] == 4
    assert payload["bookings_30d"] == 9
    assert payload["completed_revenue_30d_minor"] == 750_000
    assert payload["completed_revenue_unpriced_count"] == 1
    assert payload["revenue_currency"] == "KZT"


@pytest.mark.asyncio
async def test_customers_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    customer = SimpleNamespace(id=1, name="Ivan", phone="wa:777@c.us", muted_until=None)
    fake_session = _FakeSession(
        execute_results=[
            _ScalarsAllResult([customer]),
            _ScalarOneResult(0),
        ]
    )
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/customers?limit=500&offset=-5", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 200
    assert payload["offset"] == 0
    assert payload["items"][0]["name"] == "Ivan"


@pytest.mark.asyncio
async def test_appointments_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    appt = SimpleNamespace(
        id=11,
        scheduled_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        status="new",
        crm_appointment_id=None,
        crm_doctor_id=None,
        reminder_24h_sent_at=None,
    )
    customer = SimpleNamespace(id=2, name="Aruzhan", phone="tg:123")
    fake_session = _FakeSession(execute_results=[_AllRowsResult([(appt, customer)])])
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/appointments", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"][0]["id"] == 11
    assert payload["items"][0]["customer_name"] == "Aruzhan"
    assert payload["items"][0]["status"] == "new"


@pytest.mark.asyncio
async def test_appointments_timeline_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(id=1, name="Demo", system_prompt="", timezone="UTC")
    timeline_payload = {
        "date": "2026-06-02",
        "timezone": "UTC",
        "slot_minutes": 30,
        "slots": ["10:00", "10:30"],
        "rows": [],
    }

    async def fake_build(*_args, **_kwargs):
        return timeline_payload

    fake_session = _FakeSession(get_results={(admin_api.Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    monkeypatch.setattr(admin_api.appointment_service, "build_appointments_timeline", fake_build)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/web/appointments/timeline?date=2026-06-02",
            headers=_auth_headers(),
        )
    assert response.status_code == 200
    assert response.json()["date"] == "2026-06-02"
    assert response.json()["slots"] == ["10:00", "10:30"]


@pytest.mark.asyncio
async def test_appointments_timeline_bad_date(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(id=1, name="Demo", system_prompt="", timezone="UTC")

    async def fake_build(*_args, **_kwargs):
        raise ValueError("date must be YYYY-MM-DD")

    fake_session = _FakeSession(get_results={(admin_api.Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    monkeypatch.setattr(admin_api.appointment_service, "build_appointments_timeline", fake_build)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/web/appointments/timeline?date=not-a-date",
            headers=_auth_headers(),
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_crm_staff_list_demo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(
        id=1,
        name="Demo",
        crm_provider="demo",
        crm_base_url=None,
        crm_api_token=None,
    )
    fake_session = _FakeSession(get_results={(admin_api.Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/crm/staff", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "demo"
    assert len(payload["items"]) >= 1
    assert payload["items"][0]["id"]


@pytest.mark.asyncio
async def test_crm_staff_list_from_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(
        id=1,
        name="Clinic",
        crm_provider="amocrm",
        crm_base_url="https://crm.example",
        crm_api_token="secret",
    )
    cache = SimpleNamespace(
        organization_id=1,
        items=[
            {
                "id": "crm-42",
                "name": "Dr CRM",
                "work_start": "09:00",
                "work_end": "17:00",
                "active": True,
            }
        ],
        synced_at=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
        source="crm",
        sync_error=None,
    )
    fake_session = _FakeSession(
        get_results={
            (admin_api.Organization, 1): org,
            (OrganizationCrmStaffCache, 1): cache,
        },
    )
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/crm/staff", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "crm"
    assert payload["synced_at"] is not None
    assert len(payload["items"]) == 1
    assert payload["items"][0]["id"] == "crm-42"


@pytest.mark.asyncio
async def test_crm_staff_sync_demo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(
        id=1,
        name="Demo",
        crm_provider="demo",
        crm_base_url=None,
        crm_api_token=None,
    )
    fake_session = _FakeSession(get_results={(admin_api.Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/crm/staff/sync", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "demo"
    assert payload["count"] >= 1
    assert payload["synced_at"] is not None
    assert len(payload["items"]) >= 1


@pytest.mark.asyncio
async def test_crm_staff_sync_persists_cache(monkeypatch: pytest.MonkeyPatch):
    from bot.crm.base import StaffMember

    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(
        id=1,
        name="Clinic",
        crm_provider="amocrm",
        crm_base_url="https://crm.example",
        crm_api_token="secret",
    )

    class _FakeProvider:
        async def list_staff(self):
            return [
                StaffMember(
                    id="doc-9",
                    name="Иванов",
                    work_start="10:00",
                    work_end="19:00",
                    active=True,
                )
            ]

    fake_session = _FakeSession(get_results={(admin_api.Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    monkeypatch.setattr(crm_staff_service, "get_crm_provider", lambda _org: _FakeProvider())

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/web/crm/staff/sync", headers=_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"] == "crm"
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == "doc-9"
    assert fake_session.committed is True
    added = [obj for obj in fake_session.added if hasattr(obj, "organization_id")]
    assert len(added) == 1
    assert added[0].items[0]["name"] == "Иванов"


@pytest.mark.asyncio
async def test_prompts_get_and_update(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(id=1, name="Demo Org", system_prompt="Old", timezone="UTC")
    fake_session = _FakeSession(get_results={(admin_api.Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        get_response = await client.get("/api/web/prompts", headers=_auth_headers())
        put_response = await client.put(
            "/api/web/prompts",
            headers=_auth_headers(),
            json={"system_prompt": "  Updated prompt  "},
        )

    assert get_response.status_code == 200
    assert get_response.json()["org_name"] == "Demo Org"
    assert put_response.status_code == 200
    assert put_response.json()["system_prompt"] == "Updated prompt"
    assert fake_session.committed is True
    assert fake_session.refreshed is True


@pytest.mark.asyncio
async def test_whatsapp_settings_get_and_put(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(
        id=1,
        name="Demo Org",
        whatsapp_provider="meta",
        whatsapp_instance_id=None,
        whatsapp_api_token=None,
        whatsapp_meta_phone_number_id="12345",
        whatsapp_meta_access_token="secret-token",
        whatsapp_meta_reminder_template_name="reminder_24h",
        whatsapp_meta_reminder_template_lang="ru",
        whatsapp_broadcast_quota_monthly=100,
        whatsapp_broadcast_sent_count=12,
        whatsapp_broadcast_month_key="2026-05",
    )
    fake_session = _FakeSession(get_results={(admin_api.Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        get_response = await client.get("/api/web/whatsapp-settings", headers=_auth_headers())
        put_response = await client.put(
            "/api/web/whatsapp-settings",
            headers=_auth_headers(),
            json={
                "whatsapp_provider": "green",
                "whatsapp_instance_id": "999",
                "whatsapp_api_token": "g-token",
                "whatsapp_meta_phone_number_id": "777",
                "whatsapp_meta_access_token": "new-meta-token",
                "whatsapp_meta_reminder_template_name": "tpl_new",
                "whatsapp_meta_reminder_template_lang": "kk",
                "whatsapp_broadcast_quota_monthly": 250,
            },
        )

    assert get_response.status_code == 200
    get_payload = get_response.json()
    assert get_payload["meta_access_token_set"] is True
    assert get_payload["whatsapp_api_token_set"] is False
    assert "secret-token" not in get_response.text
    assert put_response.status_code == 200
    payload = put_response.json()
    assert payload["whatsapp_provider"] == "green"
    assert payload["whatsapp_instance_id"] == "999"
    assert payload["whatsapp_broadcast_quota_monthly"] == 250
    assert payload["whatsapp_api_token_set"] is True
    assert "new-meta-token" not in put_response.text
    assert "g-token" not in put_response.text
    assert fake_session.committed is True
    assert fake_session.refreshed is True


@pytest.mark.asyncio
async def test_broadcast_run_no_customers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = SimpleNamespace(
        id=1,
        name="Demo Org",
        whatsapp_provider="meta",
        whatsapp_instance_id=None,
        whatsapp_api_token=None,
        whatsapp_meta_phone_number_id="12345",
        whatsapp_meta_access_token="meta-token",
        whatsapp_meta_reminder_template_name=None,
        whatsapp_meta_reminder_template_lang="ru",
        whatsapp_broadcast_quota_monthly=100,
        whatsapp_broadcast_sent_count=0,
        whatsapp_broadcast_month_key="2026-05",
        whatsapp_broadcast_locked=False,
    )
    fake_session = _FakeSession(
        execute_results=[_ScalarsAllResult([])],
        get_results={(admin_api.Organization, 1): org},
    )
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/broadcast/run",
            headers=_auth_headers(),
            json={"template_name": "promo_template", "language_code": "ru", "body_parameters": []},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["reason"] == "no_whatsapp_customers"


@pytest.mark.asyncio
async def test_org_settings_get(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    org.review_2gis_url = "https://2gis.ru/demo"
    fake_session = _FakeSession(get_results={(Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/org-settings", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["bot_enabled"] is True
    assert payload["bot_operational"] is True
    assert payload["auto_confirm_appointments"] is False
    assert payload["review_2gis_url"] == "https://2gis.ru/demo"
    assert payload["bot_display_name"] is None
    assert payload["bot_welcome_message"] is None
    assert payload["bot_tone"] is None


@pytest.mark.asyncio
async def test_org_settings_patch_requires_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/api/web/org-settings",
            headers=_auth_headers(),
            json={"bot_enabled": False},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_org_settings_patch_review_2gis_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    org.review_2gis_url = None
    fake_session = _FakeSession(get_results={(Organization, 1): org})

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
            response = await client.patch(
                "/api/web/org-settings",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
                json={"review_2gis_url": "https://2gis.ru/firm/99"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    assert org.review_2gis_url == "https://2gis.ru/firm/99"
    assert response.json()["review_2gis_url"] == "https://2gis.ru/firm/99"
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_org_settings_patch_retention(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    org.retention_days_after_complete = None
    org.retention_message = None
    fake_session = _FakeSession(get_results={(Organization, 1): org})

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
            response = await client.patch(
                "/api/web/org-settings",
                headers={"Authorization": "Bearer 1234", "x-org-id": "1"},
                json={
                    "retention_days_after_complete": 30,
                    "retention_message": "Снова ждём вас в {org_name}!",
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    assert org.retention_days_after_complete == 30
    assert org.retention_message == "Снова ждём вас в {org_name}!"
    payload = response.json()
    assert payload["retention_days_after_complete"] == 30
    assert payload["retention_message"] == "Снова ждём вас в {org_name}!"


@pytest.mark.asyncio
async def test_org_settings_patch_post_service_upsell(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    org.post_service_upsell_message = None
    fake_session = _FakeSession(get_results={(Organization, 1): org})

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
            response = await client.patch(
                "/api/web/org-settings",
                headers={"Authorization": "Bearer 1234", "x-org-id": "1"},
                json={"post_service_upsell_message": "Также рекомендуем чистку в {org_name}."},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    assert org.post_service_upsell_message == "Также рекомендуем чистку в {org_name}."
    assert response.json()["post_service_upsell_message"] == "Также рекомендуем чистку в {org_name}."


@pytest.mark.asyncio
async def test_org_settings_patch_bot_customization(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    fake_session = _FakeSession(get_results={(Organization, 1): org})

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
            response = await client.patch(
                "/api/web/org-settings",
                headers={"Authorization": "Bearer 1234", "x-org-id": "1"},
                json={
                    "bot_display_name": "Айша",
                    "bot_welcome_message": "Привет!",
                    "bot_tone": "formal",
                },
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200, response.text
    assert org.bot_display_name == "Айша"
    assert org.bot_welcome_message == "Привет!"
    assert org.bot_tone == "formal"
    payload = response.json()
    assert payload["bot_display_name"] == "Айша"
    assert payload["bot_tone"] == "formal"


@pytest.mark.asyncio
async def test_org_settings_patch_bot_tone_rejects_invalid(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    fake_session = _FakeSession(get_results={(Organization, 1): org})

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
            response = await client.patch(
                "/api/web/org-settings",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
                json={"bot_tone": "sarcastic"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_org_settings_patch_review_2gis_url_rejects_http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True, billing_paid_until=None)
    org.id = 1
    fake_session = _FakeSession(get_results={(Organization, 1): org})

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
            response = await client.patch(
                "/api/web/org-settings",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
                json={"review_2gis_url": "http://insecure.example"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_admin_auth_login_success(monkeypatch: pytest.MonkeyPatch):
    admin = Admin(id=2, login="owner", password_hash="sha256$x", org_id=1)
    fake_session = _FakeSession()

    class _LoginResult:
        def scalar_one_or_none(self):
            return admin

    async def fake_execute(_stmt):
        return _LoginResult()

    fake_session.execute = fake_execute  # type: ignore[method-assign]
    async def fake_authenticate(_l, _p, *, org_id=None):
        return admin

    monkeypatch.setattr(admin_api, "authenticate_admin_login", fake_authenticate)
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    async def fake_create_session(_session, _admin_id):
        return ("sess-abc", datetime.now(timezone.utc))

    monkeypatch.setattr(admin_api, "create_admin_session", fake_create_session)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/admin/auth/login",
            json={"login": "owner", "password": "secret"},
        )
    assert response.status_code == 200
    assert response.json()["session_id"] == "sess-abc"
    assert response.json()["admin"]["org_id"] == 1


@pytest.mark.asyncio
async def test_list_admins_session_auth(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    rows = [Admin(id=1, login="a", password_hash="x", org_id=1)]
    fake_session = _FakeSession(execute_results=[_ScalarsAllResult(rows)])

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
            response = await client.get(
                "/api/web/admins",
                headers={**_auth_headers(), "X-Admin-Session": "sess"},
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


@pytest.mark.asyncio
async def test_bot_test_chat_requires_auth(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/bot-test/chat",
            json={"message": "hello", "sandbox_id": "abcd1234efgh5678"},
        )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_bot_test_chat_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")

    async def fake_get_ai_response(*, user_id, user_text, db_memory, channel, org_id=None):
        assert channel == "web"
        assert org_id == 1
        assert user_id.startswith("admin-sandbox-")
        assert user_text == "Привет"
        return "echo:Привет"

    async def fake_require_sub(_org_id: int) -> None:
        return None

    monkeypatch.setattr(admin_api.llm_engine, "get_ai_response", fake_get_ai_response)
    monkeypatch.setattr(admin_api, "_require_active_subscription", fake_require_sub)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/bot-test/chat",
            headers=_auth_headers(),
            json={"message": "Привет", "sandbox_id": "abcd1234efgh5678"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["reply"] == "echo:Привет"
    assert payload["org_id"] == 1
    assert payload["sandbox_id"] == "abcd1234efgh5678"


@pytest.mark.asyncio
async def test_bot_test_chat_subscription_inactive(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = Organization(name="Demo", bot_enabled=True)
    org.id = 1
    # Past paid-until → inactive
    org.billing_paid_until = datetime(2020, 1, 1, tzinfo=timezone.utc)
    fake_session = _FakeSession(get_results={(Organization, 1): org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    async def fail_if_called(**_kwargs):
        raise AssertionError("LLM must not run when subscription inactive")

    monkeypatch.setattr(admin_api.llm_engine, "get_ai_response", fail_if_called)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/bot-test/chat",
            headers=_auth_headers(),
            json={"message": "Привет", "sandbox_id": "abcd1234efgh5678"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "subscription_inactive"


@pytest.mark.asyncio
async def test_bot_test_reset_clears_session(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    eng = admin_api.llm_engine
    key = "web:1:admin-sandbox-abcd1234efgh5678"
    eng._sessions[key] = eng._UserSession(handle=object(), provider_name="stub")

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/web/bot-test/reset",
            headers=_auth_headers(),
            json={"sandbox_id": "abcd1234efgh5678"},
        )
    assert response.status_code == 200
    assert key not in eng._sessions

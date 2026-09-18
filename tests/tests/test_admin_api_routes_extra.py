from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.admin_api as admin_api
from bot.db.models import AppointmentStatus


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

    async def execute(self, _stmt):
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call in fake session")
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


class _SequentialSessionFactory:
    def __init__(self, sessions: list[_FakeSession]):
        self.sessions = list(sessions)

    def __call__(self):
        if not self.sessions:
            raise AssertionError("Unexpected AsyncSessionLocal() call")
        return _FakeSessionManager(self.sessions.pop(0))


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(admin_api.router, prefix="/api/web")
    return app


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer 1234", "x-org-id": "1"}


def _org(**overrides):
    base = {
        "id": 1,
        "name": "Org",
        "billing_paid_until": None,
        "whatsapp_provider": None,
        "whatsapp_instance_id": None,
        "whatsapp_api_token": None,
        "whatsapp_meta_phone_number_id": None,
        "whatsapp_meta_access_token": None,
        "whatsapp_meta_reminder_template_name": None,
        "whatsapp_meta_reminder_template_lang": "ru",
        "whatsapp_broadcast_quota_monthly": 100,
        "whatsapp_broadcast_sent_count": 0,
        "whatsapp_broadcast_month_key": datetime.now(timezone.utc).strftime("%Y-%m"),
        "whatsapp_broadcast_locked": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_stats_activity_success(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    now = datetime.now(timezone.utc)
    fake_session = _FakeSession(
        execute_results=[
            _AllRowsResult([(now, AppointmentStatus.NEW, 2)]),
            _AllRowsResult([(now, AppointmentStatus.CONFIRMED, 3)]),
            _AllRowsResult([(now, AppointmentStatus.COMPLETED, 1)]),
            _AllRowsResult([(now, AppointmentStatus.CANCELLED, 1)]),
        ]
    )
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/web/stats/activity", headers=_auth_headers())
    assert response.status_code == 200
    payload = response.json()
    assert "day" in payload and "week" in payload and "month" in payload and "year" in payload
    assert len(payload["day"]) == 30


@pytest.mark.asyncio
async def test_organizations_billing_and_logs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    org = _org(id=1, name="Acme")
    orgs_session = _FakeSession(execute_results=[_ScalarsAllResult([org])])
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(orgs_session))
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        orgs_resp = await client.get("/api/web/organizations", headers=_auth_headers())
    assert orgs_resp.status_code == 200
    assert orgs_resp.json()["items"][0]["name"] == "Acme"

    billing_org = _org(id=2, name="Billing")
    billing_session = _FakeSession(get_results={(admin_api.Organization, 2): billing_org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(billing_session))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        billing_resp = await client.put(
            "/api/web/organizations/2/billing",
            headers={"Authorization": "Bearer 1234"},
            json={"billing_paid_until": "2030-01-01T00:00:00+00:00"},
        )
    assert billing_resp.status_code == 200
    assert billing_session.committed is True

    logs_org = _org(id=3, name="Logs")
    log_row = SimpleNamespace(
        id=11,
        channel="whatsapp",
        external_user_id="777@c.us",
        user_message_preview="hi",
        reply_preview="hello",
        status="ok",
        error_hint=None,
        created_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    logs_session = _FakeSession(
        execute_results=[_ScalarsAllResult([log_row])],
        get_results={(admin_api.Organization, 3): logs_org},
    )
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(logs_session))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        logs_resp = await client.get("/api/web/interaction-logs", headers={**_auth_headers(), "x-org-id": "3"})
    assert logs_resp.status_code == 200
    assert logs_resp.json()["items"][0]["channel"] == "whatsapp"


@pytest.mark.asyncio
async def test_broadcast_run_guard_paths(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "1234")
    monkeypatch.setattr(admin_api, "_month_key_now", lambda: "2030-01")
    app = _test_app()

    locked_session = _FakeSession(get_results={(admin_api.Organization, 1): _org(whatsapp_broadcast_locked=True)})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(locked_session))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        locked_resp = await client.post("/api/web/broadcast/run", headers=_auth_headers(), json={"template_name": "tpl"})
    assert locked_resp.status_code == 409

    quota_org = _org(
        whatsapp_broadcast_sent_count=95,
        whatsapp_broadcast_quota_monthly=100,
        whatsapp_broadcast_month_key="2030-01",
    )
    quota_session = _FakeSession(
        execute_results=[_ScalarsAllResult([SimpleNamespace(id=1, phone="wa:777@c.us")] * 6)],
        get_results={(admin_api.Organization, 1): quota_org},
    )
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", lambda: _FakeSessionManager(quota_session))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        quota_resp = await client.post("/api/web/broadcast/run", headers=_auth_headers(), json={"template_name": "tpl"})
    assert quota_resp.status_code == 400

    meta_org = _org(
        whatsapp_provider="meta",
        whatsapp_meta_phone_number_id="pnid",
        whatsapp_meta_access_token="token",
    )
    main_session = _FakeSession(
        execute_results=[_ScalarsAllResult([SimpleNamespace(id=1, phone="wa:777@c.us")])],
        get_results={(admin_api.Organization, 1): meta_org},
    )
    finally_session = _FakeSession(get_results={(admin_api.Organization, 1): meta_org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", _SequentialSessionFactory([main_session, finally_session]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        meta_resp = await client.post("/api/web/broadcast/run", headers=_auth_headers(), json={"language_code": "ru"})
    assert meta_resp.status_code == 400
    assert meta_org.whatsapp_broadcast_locked is False
    assert finally_session.committed is True

    green_org = _org(whatsapp_provider="green", whatsapp_instance_id="1", whatsapp_api_token="tok")
    main_session2 = _FakeSession(
        execute_results=[_ScalarsAllResult([SimpleNamespace(id=1, phone="wa:777@c.us")])],
        get_results={(admin_api.Organization, 1): green_org},
    )
    finally_session2 = _FakeSession(get_results={(admin_api.Organization, 1): green_org})
    monkeypatch.setattr(admin_api, "AsyncSessionLocal", _SequentialSessionFactory([main_session2, finally_session2]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        green_resp = await client.post("/api/web/broadcast/run", headers=_auth_headers(), json={"template_name": "ignored"})
    assert green_resp.status_code == 400

"""Wave 2 D.1: Trial provisioning E2E — billing_paid_until, idempotent confirm, handoff."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

import web.user_api as user_api
from bot.db.models import (
    Admin,
    Organization,
    PaymentStatus,
    ServiceCatalog,
    ServiceRequest,
    ServiceRequestStatus,
)
from test_t4_self_service_provisioning import (
    _FakeSessionCtx,
    _ProvisionFakeSession,
    _ScalarOneOrNoneResult,
    _ScalarsFirstResult,
    _app,
    _trial_service,
    _user,
)


def _billing_delta_ok(until: datetime) -> bool:
    now = datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    delta = until - now
    return timedelta(days=13, hours=23) <= delta <= timedelta(days=14, hours=1)


@pytest.mark.asyncio
async def test_d1_confirm_provisions_trial_billing_until(monkeypatch: pytest.MonkeyPatch):
    user = _user()
    service = _trial_service()
    req = ServiceRequest(
        id=10,
        user_id=1,
        service_id=3,
        status=ServiceRequestStatus.REQUESTED,
        payment_status=PaymentStatus.PENDING,
        total_minor=0,
        currency="KZT",
        checkout_id="chk-d1-1",
        meta_json={"clinic_name": "D1 Trial Clinic"},
    )
    tx = SimpleNamespace(
        id=99,
        status=PaymentStatus.PENDING,
        paid_at=None,
        failure_reason=None,
    )
    session = _ProvisionFakeSession(
        get_results={
            (ServiceRequest, 10): req,
            (ServiceCatalog, 3): service,
        },
        execute_results=[
            _ScalarOneOrNoneResult(None),
            _ScalarOneOrNoneResult(None),
        ],
    )
    session._execute_results.insert(0, _ScalarsFirstResult(tx))

    async def fake_require_user(_sid):
        return user

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(session))

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/web/user/checkout/10/confirm",
            json={"success": True},
            headers={"x-user-session": "ok"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["organization_id"] == 20
    assert payload["admin_login"] == "owner@clinic.test"
    assert payload["provisioned"] is True
    assert req.status == ServiceRequestStatus.COMPLETED
    assert req.provisioned_org_id == 20

    orgs = [obj for obj in session.added if isinstance(obj, Organization)]
    assert len(orgs) == 1
    org = orgs[0]
    assert org.owner_user_id == 1
    assert org.crm_provider == "none"
    assert org.billing_paid_until is not None
    assert _billing_delta_ok(org.billing_paid_until)


@pytest.mark.asyncio
async def test_d1_checkout_chain_idempotent(monkeypatch: pytest.MonkeyPatch):
    user = _user()
    service = _trial_service()
    org = Organization(id=20, name="D1 Clinic", timezone="UTC", owner_user_id=1)
    admin = Admin(id=200, login="owner@clinic.test", password_hash="hash", org_id=20)
    req = ServiceRequest(
        id=10,
        user_id=1,
        service_id=3,
        status=ServiceRequestStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
        total_minor=0,
        currency="KZT",
        checkout_id="chk-d1-2",
        meta_json={"clinic_name": "D1 Clinic"},
        provisioned_org_id=20,
    )
    tx = SimpleNamespace(id=99, status=PaymentStatus.PAID, paid_at=None, failure_reason=None)
    session = _ProvisionFakeSession(
        get_results={
            (ServiceRequest, 10): req,
            (ServiceCatalog, 3): service,
            (Organization, 20): org,
        },
        execute_results=[_ScalarsFirstResult(tx), _ScalarOneOrNoneResult(admin)],
    )

    async def fake_require_user(_sid):
        return user

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(session))

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/web/user/checkout/10/confirm",
            json={"success": True},
            headers={"x-user-session": "ok"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["organization_id"] == 20
    assert payload["provisioned"] is False
    assert payload["admin_login"] == "owner@clinic.test"


@pytest.mark.asyncio
async def test_d1_admin_handoff_after_provision(monkeypatch: pytest.MonkeyPatch):
    org = Organization(
        id=20,
        name="D1 Handoff Clinic",
        timezone="UTC",
        owner_user_id=1,
        billing_paid_until=datetime.now(timezone.utc) + timedelta(days=14),
    )
    admin = Admin(id=200, login="owner@clinic.test", password_hash="hash", org_id=20)
    session = _ProvisionFakeSession(
        execute_results=[
            _ScalarOneOrNoneResult(org),
            _ScalarOneOrNoneResult(admin),
        ]
    )

    async def fake_create_admin_session(_session, _admin_id):
        return ("adm-d1-sess", datetime(2030, 1, 1, tzinfo=timezone.utc))

    async def fake_require_user(_sid):
        return _user()

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(session))
    monkeypatch.setattr(user_api, "create_admin_session", fake_create_admin_session)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/web/user/auth/admin-handoff",
            headers={"x-user-session": "ok"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "adm-d1-sess"
    assert body["admin"]["org_id"] == 20
    assert body["admin"]["login"] == "owner@clinic.test"

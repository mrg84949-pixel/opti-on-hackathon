from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.user_api as user_api
from bot.db.models import (
    Admin,
    Organization,
    PaymentStatus,
    ServiceCatalog,
    ServiceRequest,
    ServiceRequestStatus,
    UserAccount,
)


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsFirstResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


class _ProvisionFakeSession:
    def __init__(self, *, get_results=None, execute_results=None, next_org_id=20, next_admin_id=200):
        self._get_results = dict(get_results or {})
        self._execute_results = list(execute_results or [])
        self.added: list[object] = []
        self.committed = False
        self._next_org_id = next_org_id
        self._next_admin_id = next_admin_id

    async def get(self, model, key):
        return self._get_results.get((model, key))

    async def execute(self, _stmt):
        if self._execute_results:
            return self._execute_results.pop(0)
        return _ScalarOneOrNoneResult(None)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, Organization) and getattr(obj, "id", None) is None:
                obj.id = self._next_org_id
                self._next_org_id += 1
            if isinstance(obj, Admin) and getattr(obj, "id", None) is None:
                obj.id = self._next_admin_id
                self._next_admin_id += 1

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionCtx:
    def __init__(self, session: _ProvisionFakeSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(user_api.router, prefix="/api/web/user")
    return app


def _user(user_id: int = 1, email: str = "owner@clinic.test") -> UserAccount:
    return UserAccount(
        id=user_id,
        email=email,
        password_hash="hash:secret",
        is_active=True,
        email_verified=False,
    )


def _trial_service() -> ServiceCatalog:
    return ServiceCatalog(
        id=3,
        slug="optibot-trial",
        name="Opti-Bot Trial",
        price_minor=0,
        currency="KZT",
        is_active=True,
    )


def _consult_service() -> ServiceCatalog:
    return ServiceCatalog(
        id=1,
        slug="consultation",
        name="Consultation",
        price_minor=100,
        currency="KZT",
        is_active=True,
    )


@pytest.mark.asyncio
async def test_confirm_checkout_provisions_org_and_admin(monkeypatch: pytest.MonkeyPatch):
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
        checkout_id="chk-1",
        meta_json={"clinic_name": "Smile Clinic"},
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
    assert orgs[0].crm_provider == "none"


@pytest.mark.asyncio
async def test_confirm_checkout_idempotent(monkeypatch: pytest.MonkeyPatch):
    user = _user()
    service = _trial_service()
    org = Organization(id=20, name="Smile Clinic", timezone="UTC", owner_user_id=1)
    admin = Admin(id=200, login="owner@clinic.test", password_hash="hash", org_id=20)
    req = ServiceRequest(
        id=10,
        user_id=1,
        service_id=3,
        status=ServiceRequestStatus.COMPLETED,
        payment_status=PaymentStatus.PAID,
        total_minor=0,
        currency="KZT",
        checkout_id="chk-1",
        meta_json={"clinic_name": "Smile Clinic"},
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


@pytest.mark.asyncio
async def test_non_provisioning_slug_no_org(monkeypatch: pytest.MonkeyPatch):
    service = _consult_service()
    req = ServiceRequest(
        id=10,
        user_id=1,
        service_id=1,
        status=ServiceRequestStatus.REQUESTED,
        payment_status=PaymentStatus.PENDING,
        total_minor=100,
        currency="KZT",
        checkout_id="chk-2",
        meta_json={"notes": ""},
    )
    tx = SimpleNamespace(id=99, status=PaymentStatus.PENDING, paid_at=None, failure_reason=None)
    session = _ProvisionFakeSession(
        get_results={(ServiceRequest, 10): req, (ServiceCatalog, 1): service},
        execute_results=[_ScalarsFirstResult(tx)],
    )

    async def fake_require_user(_sid):
        return _user()

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
    assert payload["organization_id"] is None
    assert payload["provisioned"] is False
    assert req.status == ServiceRequestStatus.PROCESSING


@pytest.mark.asyncio
async def test_confirm_checkout_second_org_for_same_user_409(monkeypatch: pytest.MonkeyPatch):
    user = _user()
    service = _trial_service()
    existing_org = Organization(id=5, name="Existing", timezone="UTC", owner_user_id=1)
    req = ServiceRequest(
        id=10,
        user_id=1,
        service_id=3,
        status=ServiceRequestStatus.REQUESTED,
        payment_status=PaymentStatus.PENDING,
        total_minor=0,
        currency="KZT",
        checkout_id="chk-3",
        meta_json={"clinic_name": "New Clinic"},
    )
    tx = SimpleNamespace(id=99, status=PaymentStatus.PENDING, paid_at=None, failure_reason=None)
    session = _ProvisionFakeSession(
        get_results={(ServiceRequest, 10): req, (ServiceCatalog, 3): service},
        execute_results=[_ScalarsFirstResult(tx), _ScalarOneOrNoneResult(existing_org)],
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

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_admin_handoff_requires_owner(monkeypatch: pytest.MonkeyPatch):
    session = _ProvisionFakeSession(execute_results=[_ScalarOneOrNoneResult(None)])
    async def fake_require_user(_sid):
        return _user()

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(session))

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post("/api/web/user/auth/admin-handoff", headers={"x-user-session": "ok"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_handoff_success(monkeypatch: pytest.MonkeyPatch):
    org = Organization(id=20, name="Clinic", timezone="UTC", owner_user_id=1)
    admin = Admin(id=200, login="owner@clinic.test", password_hash="hash", org_id=20)
    session = _ProvisionFakeSession(
        execute_results=[
            _ScalarOneOrNoneResult(org),
            _ScalarOneOrNoneResult(admin),
        ]
    )

    async def fake_create_admin_session(_session, _admin_id):
        return ("adm-sess", datetime(2030, 1, 1, tzinfo=timezone.utc))

    async def fake_require_user(_sid):
        return _user()

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(session))
    monkeypatch.setattr(user_api, "create_admin_session", fake_create_admin_session)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post("/api/web/user/auth/admin-handoff", headers={"x-user-session": "ok"})

    assert response.status_code == 200
    assert response.json()["session_id"] == "adm-sess"
    assert response.json()["admin"]["org_id"] == 20


@pytest.mark.asyncio
async def test_create_service_request_requires_clinic_name_for_trial(monkeypatch: pytest.MonkeyPatch):
    service = _trial_service()
    session = _ProvisionFakeSession(
        get_results={
            (ServiceCatalog, 3): service,
        }
    )

    async def fake_require_user(_sid):
        return _user()

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(session))

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/web/user/services/3/request",
            json={},
            headers={"x-user-session": "ok"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "clinic_name is required for this service"


@pytest.mark.asyncio
async def test_auth_me_includes_owned_org(monkeypatch: pytest.MonkeyPatch):
    org = Organization(id=20, name="Owned Clinic", timezone="UTC", owner_user_id=1)
    session = _ProvisionFakeSession(execute_results=[_ScalarOneOrNoneResult(org)])
    async def fake_require_user(_sid):
        return _user()

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(session))

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/api/web/user/auth/me", headers={"x-user-session": "ok"})

    assert response.status_code == 200
    owned = response.json()["owned_organization"]
    assert owned["id"] == 20
    assert owned["name"] == "Owned Clinic"
    assert owned["subscription_active"] is True
    assert owned["billing_paid_until"] is None


@pytest.mark.asyncio
async def test_create_service_request_reuses_pending_trial(monkeypatch: pytest.MonkeyPatch):
    service = _trial_service()
    existing = ServiceRequest(
        id=77,
        user_id=1,
        service_id=service.id,
        status=ServiceRequestStatus.REQUESTED,
        payment_status=PaymentStatus.PENDING,
        total_minor=0,
        currency="KZT",
        checkout_id="chk-existing",
        meta_json={"clinic_name": "Clinic A"},
        provisioned_org_id=None,
    )
    session = _ProvisionFakeSession(
        get_results={(ServiceCatalog, 3): service},
        execute_results=[_ScalarOneOrNoneResult(None), _ScalarOneOrNoneResult(existing)],
    )

    async def fake_require_user(_sid):
        return _user()

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(session))

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/web/user/services/3/request",
            json={"clinic_name": "Clinic A"},
            headers={"x-user-session": "ok"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 77
    assert payload["checkout_id"] == "chk-existing"
    assert payload["reused"] is True
    assert not any(isinstance(obj, ServiceRequest) for obj in session.added)


@pytest.mark.asyncio
async def test_create_service_request_409_when_user_already_owns_org(monkeypatch: pytest.MonkeyPatch):
    service = _trial_service()
    owned_org = Organization(id=20, name="Owned", timezone="UTC", owner_user_id=1)
    session = _ProvisionFakeSession(
        get_results={(ServiceCatalog, 3): service},
        execute_results=[_ScalarOneOrNoneResult(owned_org)],
    )

    async def fake_require_user(_sid):
        return _user()

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(session))

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/web/user/services/3/request",
            json={"clinic_name": "New Clinic"},
            headers={"x-user-session": "ok"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "User already owns an organization"

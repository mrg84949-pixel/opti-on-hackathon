from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.user_api as user_api


class _ScalarOneNoneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _ScalarsFirstResult:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value


class _AllRowsResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _FakeSession:
    def __init__(self, *, execute_results=None, get_results=None):
        self._execute_results = list(execute_results or [])
        self._get_results = dict(get_results or {})
        self.added = []
        self.committed = False

    async def execute(self, _stmt):
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    async def get(self, model, key):
        return self._get_results.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for idx, obj in enumerate(self.added, start=1):
            if getattr(obj, "id", None) is None:
                obj.id = idx

    async def commit(self):
        self.committed = True


class _FakeSessionCtx:
    def __init__(self, session: _FakeSession):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(user_api.router, prefix="/api/web/user")
    return app


@pytest.mark.asyncio
async def test_require_user_branches(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    with pytest.raises(Exception):
        await user_api._require_user(None)

    expired_session = _FakeSession(
        execute_results=[_ScalarOneNoneResult(SimpleNamespace(user_id=1, expires_at=now))]
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(expired_session))
    monkeypatch.setattr(user_api, "now_utc", lambda: now)
    with pytest.raises(Exception):
        await user_api._require_user("expired")

    inactive_user_session = _FakeSession(
        execute_results=[_ScalarOneNoneResult(SimpleNamespace(user_id=1, expires_at=now + timedelta(hours=1)))],
        get_results={(user_api.UserAccount, 1): SimpleNamespace(id=1, is_active=False)},
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(inactive_user_session))
    with pytest.raises(Exception):
        await user_api._require_user("inactive")

    active_user = SimpleNamespace(id=2, is_active=True, email="ok@example.com")
    valid_session = _FakeSession(
        execute_results=[_ScalarOneNoneResult(SimpleNamespace(user_id=2, expires_at=now + timedelta(hours=1)))],
        get_results={(user_api.UserAccount, 2): active_user},
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(valid_session))
    assert await user_api._require_user("valid") is active_user


@pytest.mark.asyncio
async def test_register_duplicate_and_success(monkeypatch: pytest.MonkeyPatch):
    dup_session = _FakeSession(execute_results=[_ScalarOneNoneResult(SimpleNamespace(id=1))])
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(dup_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        dup = await client.post(
            "/api/web/user/auth/register",
            json={"email": "dup@example.com", "password": "password123", "full_name": "Dup"},
        )
    assert dup.status_code == 409

    sent = {}
    success_session = _FakeSession(execute_results=[_ScalarOneNoneResult(None)])
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(success_session))
    monkeypatch.setattr(user_api, "hash_password", lambda password: f"hash:{password}")
    monkeypatch.setattr(user_api, "issue_token", lambda: "verify-token")
    monkeypatch.setattr(user_api, "hash_secret", lambda value: f"hashed:{value}")

    async def fake_create_session(_session, user_id):
        return f"session-{user_id}", datetime(2030, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(user_api, "create_session", fake_create_session)
    monkeypatch.setattr(
        user_api,
        "send_email",
        lambda **kwargs: sent.update(kwargs) or True,
    )
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        ok = await client.post(
            "/api/web/user/auth/register",
            json={"email": "ok@example.com", "password": "password123", "full_name": "Ok"},
        )
    assert ok.status_code == 200
    payload = ok.json()
    assert payload["status"] == "registered"
    assert payload["session_id"] == "session-1"
    assert sent["to_email"] == "ok@example.com"
    assert success_session.committed is True


@pytest.mark.asyncio
async def test_login_invalid_and_success(monkeypatch: pytest.MonkeyPatch):
    bad_session = _FakeSession(execute_results=[_ScalarOneNoneResult(None)])
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(bad_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        bad = await client.post(
            "/api/web/user/auth/login",
            json={"email": "bad@example.com", "password": "wrong-password"},
        )
    assert bad.status_code == 401

    user = SimpleNamespace(id=2, email="ok@example.com", email_verified=True, password_hash="stored")
    ok_session = _FakeSession(execute_results=[_ScalarOneNoneResult(user)])
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(ok_session))
    monkeypatch.setattr(user_api, "verify_password", lambda raw, stored: raw == "password123" and stored == "stored")

    async def fake_create_session(_session, user_id):
        return f"session-{user_id}", datetime(2030, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(user_api, "create_session", fake_create_session)
    monkeypatch.setattr(user_api, "now_utc", lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        ok = await client.post(
            "/api/web/user/auth/login",
            json={"email": "ok@example.com", "password": "password123"},
        )
    assert ok.status_code == 200
    assert ok.json()["session_id"] == "session-2"
    assert user.last_login_at == datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_verify_request_confirm_and_password_reset_flows(monkeypatch: pytest.MonkeyPatch):
    unknown_session = _FakeSession(execute_results=[_ScalarOneNoneResult(None), _ScalarOneNoneResult(None)])
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(unknown_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        verify_unknown = await client.post("/api/web/user/auth/verify-email/request", json={"email": "ghost@example.com"})
        forgot_unknown = await client.post("/api/web/user/auth/forgot-password", json={"email": "ghost@example.com"})
    assert verify_unknown.status_code == 200
    assert forgot_unknown.status_code == 200

    token_row = SimpleNamespace(user_id=9, used_at=None, expires_at=datetime(2030, 1, 2, tzinfo=timezone.utc))
    missing_user_session = _FakeSession(
        execute_results=[_ScalarOneNoneResult(token_row)],
        get_results={(user_api.UserAccount, 9): None},
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(missing_user_session))
    monkeypatch.setattr(user_api, "hash_secret", lambda token: f"hashed:{token}")
    monkeypatch.setattr(user_api, "now_utc", lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        missing_user = await client.post("/api/web/user/auth/verify-email/confirm", json={"token": "code"})
    assert missing_user.status_code == 404

    user = SimpleNamespace(id=3, email_verified=False, email_verified_at=None)
    verify_row = SimpleNamespace(user_id=3, used_at=None, expires_at=datetime(2030, 1, 2, tzinfo=timezone.utc))
    verify_session = _FakeSession(
        execute_results=[_ScalarOneNoneResult(verify_row)],
        get_results={(user_api.UserAccount, 3): user},
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(verify_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        verified = await client.post("/api/web/user/auth/verify-email/confirm", json={"token": "code"})
    assert verified.status_code == 200
    assert user.email_verified is True

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        short_pw = await client.post(
            "/api/web/user/auth/reset-password",
            json={"email": "ok@example.com", "code": "123456", "new_password": "short"},
        )
        bad_email = await client.post(
            "/api/web/user/auth/reset-password",
            json={"email": "bad", "code": "123456", "new_password": "password123"},
        )
        bad_code = await client.post(
            "/api/web/user/auth/reset-password",
            json={"email": "ok@example.com", "code": "12", "new_password": "password123"},
        )
    assert short_pw.status_code == 400
    assert bad_email.status_code == 400
    assert bad_code.status_code == 400

    reset_user = SimpleNamespace(id=5, password_hash="old")
    reset_row = SimpleNamespace(used_at=None, expires_at=datetime(2030, 1, 2, tzinfo=timezone.utc))
    reset_session = _FakeSession(
        execute_results=[_ScalarOneNoneResult(reset_user), _ScalarsFirstResult(reset_row)]
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(reset_session))
    monkeypatch.setattr(user_api, "hash_password", lambda password: f"hash:{password}")
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        reset_ok = await client.post(
            "/api/web/user/auth/reset-password",
            json={"email": "ok@example.com", "code": "123456", "new_password": "password123"},
        )
    assert reset_ok.status_code == 200
    assert reset_user.password_hash == "hash:password123"
    assert reset_row.used_at == datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_service_and_checkout_negative_and_status_paths(monkeypatch: pytest.MonkeyPatch):
    async def fake_require_user(_session_id):
        return SimpleNamespace(id=1)

    monkeypatch.setattr(user_api, "_require_user", fake_require_user)

    missing_service_session = _FakeSession(get_results={(user_api.ServiceCatalog, 1): None})
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(missing_service_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        missing_service = await client.post("/api/web/user/services/1/request", json={}, headers={"x-user-session": "ok"})
    assert missing_service.status_code == 404

    missing_request_session = _FakeSession(get_results={(user_api.ServiceRequest, 10): None})
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(missing_request_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        missing_checkout = await client.post("/api/web/user/checkout/10/create", headers={"x-user-session": "ok"})
    assert missing_checkout.status_code == 404

    req = SimpleNamespace(
        id=10,
        user_id=1,
        total_minor=12000,
        currency="KZT",
        payment_status=user_api.PaymentStatus.PENDING,
        status=user_api.ServiceRequestStatus.REQUESTED,
    )
    no_tx_session = _FakeSession(
        execute_results=[_ScalarsFirstResult(None)],
        get_results={(user_api.ServiceRequest, 10): req},
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(no_tx_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        no_tx = await client.post(
            "/api/web/user/checkout/10/confirm",
            headers={"x-user-session": "ok"},
            json={"success": True},
        )
    assert no_tx.status_code == 400

    tx = SimpleNamespace(id=3, status=user_api.PaymentStatus.PENDING, paid_at=None, failure_reason=None)
    fail_req = SimpleNamespace(
        id=11,
        user_id=1,
        total_minor=12000,
        currency="KZT",
        payment_status=user_api.PaymentStatus.PENDING,
        status=user_api.ServiceRequestStatus.REQUESTED,
    )
    fail_session = _FakeSession(
        execute_results=[_ScalarsFirstResult(tx)],
        get_results={(user_api.ServiceRequest, 11): fail_req},
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(fail_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        fail_confirm = await client.post(
            "/api/web/user/checkout/11/confirm",
            headers={"x-user-session": "ok"},
            json={"success": False, "failure_reason": "declined"},
        )
    assert fail_confirm.status_code == 200
    assert fail_confirm.json()["payment_status"] == "failed"

    status_req = SimpleNamespace(
        id=12,
        user_id=1,
        total_minor=8000,
        currency="KZT",
        payment_status=user_api.PaymentStatus.PAID,
        status=user_api.ServiceRequestStatus.PROCESSING,
    )
    status_tx = SimpleNamespace(status=user_api.PaymentStatus.PAID)
    status_session = _FakeSession(
        execute_results=[_ScalarsFirstResult(status_tx)],
        get_results={(user_api.ServiceRequest, 12): status_req},
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(status_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        status_resp = await client.get("/api/web/user/checkout/12/status", headers={"x-user-session": "ok"})
    assert status_resp.status_code == 200
    assert status_resp.json()["transaction_status"] == "paid"

    my_requests_session = _FakeSession(
        execute_results=[
            _AllRowsResult(
                [
                    (
                        SimpleNamespace(
                            id=20,
                            total_minor=5000,
                            currency="KZT",
                            status=user_api.ServiceRequestStatus.PROCESSING,
                            payment_status=user_api.PaymentStatus.PAID,
                            checkout_id="chk_1",
                            provisioned_org_id=None,
                        ),
                        SimpleNamespace(name="Consultation"),
                    )
                ]
            )
        ]
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(my_requests_session))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        my_requests = await client.get("/api/web/user/my/requests", headers={"x-user-session": "ok"})
    assert my_requests.status_code == 200
    assert my_requests.json()["items"][0]["service_name"] == "Consultation"

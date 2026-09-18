from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import web.user_api as user_api


class _ScalarResult:
    def __init__(self, item):
        self._item = item

    def scalars(self):
        return self

    def first(self):
        return self._item


class _FakeSession:
    def __init__(self):
        self.req = SimpleNamespace(
            id=10,
            user_id=1,
            service_id=1,
            total_minor=12000,
            currency="KZT",
            payment_status=user_api.PaymentStatus.PENDING,
            status=user_api.ServiceRequestStatus.REQUESTED,
            provisioned_org_id=None,
            meta_json={},
        )
        self.tx = None
        self.service = SimpleNamespace(id=1, slug="consultation", name="Consultation")

    async def get(self, model, _id):
        if model is user_api.ServiceRequest:
            return self.req
        if model is user_api.ServiceCatalog:
            return self.service
        return None

    async def execute(self, _stmt):
        return _ScalarResult(self.tx)

    def add(self, obj):
        if isinstance(obj, user_api.PaymentTransaction):
            self.tx = obj
            self.tx.id = 3

    async def flush(self):
        return None

    async def commit(self):
        return None


class _FakeCtx:
    def __init__(self, s):
        self.s = s

    async def __aenter__(self):
        return self.s

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(user_api.router, prefix="/api/web/user")
    return app


@pytest.mark.asyncio
async def test_checkout_create_and_confirm(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeSession()
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeCtx(fake))

    async def _fake_require_user(_session_id: str | None):
        return SimpleNamespace(id=1)

    monkeypatch.setattr(user_api, "_require_user", _fake_require_user)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        create_resp = await client.post("/api/web/user/checkout/10/create", headers={"x-user-session": "ok"})
        confirm_resp = await client.post(
            "/api/web/user/checkout/10/confirm",
            headers={"x-user-session": "ok"},
            json={"success": True},
        )
    assert create_resp.status_code == 200
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["payment_status"] == "paid"

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

import main
import web.contact_api as contact_api
import web.user_api as user_api
from bot.config import settings
from bot.services import rate_limit as rate_limit_module


def _user_app() -> FastAPI:
    app = FastAPI()

    async def _rate_limit_handler(_request, exc: rate_limit_module.RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests", "retry_after": exc.retry_after},
            headers={"Retry-After": str(exc.retry_after)},
        )

    app.add_exception_handler(rate_limit_module.RateLimitExceeded, _rate_limit_handler)
    app.include_router(user_api.router, prefix="/api/web/user")
    return app


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, execute_results):
        self.execute_results = list(execute_results)

    async def execute(self, _stmt):
        return self.execute_results.pop(0)

    async def commit(self):
        return None


class _FakeSessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.fixture(autouse=True)
def _reset_limiter():
    rate_limit_module.reset_for_tests()
    yield
    rate_limit_module.reset_for_tests()


@pytest.mark.asyncio
async def test_limiter_allows_up_to_limit():
    limiter = rate_limit_module.InMemoryRateLimiter()
    for _ in range(3):
        assert await limiter.check("k", limit=3, window_seconds=60) is None
    retry = await limiter.check("k", limit=3, window_seconds=60)
    assert retry is not None
    assert retry >= 1


def test_client_ip_prefers_x_forwarded_for():
    request = SimpleNamespace(
        headers={"x-forwarded-for": "203.0.113.1, 10.0.0.1"},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    assert rate_limit_module.client_ip(request) == "203.0.113.1"


def test_client_ip_falls_back_to_client_host():
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    assert rate_limit_module.client_ip(request) == "127.0.0.1"


@pytest.mark.asyncio
async def test_login_returns_429_after_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_auth_ip_limit", 2)
    monkeypatch.setattr(settings, "rate_limit_auth_email_limit", 100)

    bad_session = _FakeSession(execute_results=[_ScalarOneOrNoneResult(None), _ScalarOneOrNoneResult(None)])
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(bad_session))

    async with AsyncClient(transport=ASGITransport(app=_user_app()), base_url="http://test") as client:
        first = await client.post(
            "/api/web/user/auth/login",
            json={"email": "user@example.com", "password": "wrong"},
            headers={"x-forwarded-for": "198.51.100.10"},
        )
        second = await client.post(
            "/api/web/user/auth/login",
            json={"email": "user@example.com", "password": "wrong"},
            headers={"x-forwarded-for": "198.51.100.10"},
        )
        third = await client.post(
            "/api/web/user/auth/login",
            json={"email": "user@example.com", "password": "wrong"},
            headers={"x-forwarded-for": "198.51.100.10"},
        )

    assert first.status_code == 401
    assert second.status_code == 401
    assert third.status_code == 429
    body = third.json()
    assert body["detail"] == "Too many requests"
    assert body["retry_after"] >= 1
    assert third.headers.get("retry-after")


@pytest.mark.asyncio
async def test_contact_middleware_returns_429(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTACT_FORM_TO_EMAIL", "hello@example.com")
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_contact_ip_limit", 1)
    monkeypatch.setattr(contact_api, "send_email", lambda **kwargs: True)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
        payload = {"name": "Test", "phone": "+77001234567", "comment": "hello"}
        first = await client.post(
            "/api/web/contact",
            json=payload,
            headers={"x-forwarded-for": "203.0.113.50"},
        )
        second = await client.post(
            "/api/web/contact",
            json=payload,
            headers={"x-forwarded-for": "203.0.113.50"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["retry_after"] >= 1


@pytest.mark.asyncio
async def test_rate_limit_disabled_allows_burst(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    monkeypatch.setattr(settings, "rate_limit_auth_ip_limit", 1)

    bad_session = _FakeSession(
        execute_results=[_ScalarOneOrNoneResult(None), _ScalarOneOrNoneResult(None), _ScalarOneOrNoneResult(None)]
    )
    monkeypatch.setattr(user_api, "AsyncSessionLocal", lambda: _FakeSessionCtx(bad_session))

    async with AsyncClient(transport=ASGITransport(app=_user_app()), base_url="http://test") as client:
        for _ in range(3):
            response = await client.post(
                "/api/web/user/auth/login",
                json={"email": "user@example.com", "password": "wrong"},
                headers={"x-forwarded-for": "198.51.100.99"},
            )
            assert response.status_code == 401

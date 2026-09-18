from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

import main


class _FakeSession:
    def __init__(self, *, should_fail: bool = False):
        self.should_fail = should_fail

    async def execute(self, _stmt):
        if self.should_fail:
            raise RuntimeError("db down")
        return 1


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _app() -> FastAPI:
    return main.app


def test_runtime_config_errors(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEFAULT_ORG_ID", "1")
    monkeypatch.setenv("USER_AUTH_SECRET", "super-secret")
    monkeypatch.setattr(main.settings, "tenant_config_strict", False)
    monkeypatch.setattr(main.settings, "whatsapp_verify_token", "")
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", "")
    assert main._runtime_config_errors() == []

    monkeypatch.setenv("DEFAULT_ORG_ID", "0")
    monkeypatch.setenv("USER_AUTH_SECRET", "change-me")
    errors = main._runtime_config_errors()
    assert any("DEFAULT_ORG_ID" in item for item in errors)
    assert any("USER_AUTH_SECRET" in item for item in errors)


def test_runtime_config_errors_wa_meta_secret_when_verify_token_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEFAULT_ORG_ID", "1")
    monkeypatch.setenv("USER_AUTH_SECRET", "super-secret")
    monkeypatch.setattr(main.settings, "tenant_config_strict", False)
    monkeypatch.setattr(main.settings, "whatsapp_verify_token", "verify-token")
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", "")
    errors = main._runtime_config_errors()
    assert any("WHATSAPP_APP_SECRET" in item for item in errors)

    monkeypatch.setattr(main.settings, "whatsapp_app_secret", "app-secret")
    assert not any("WHATSAPP_APP_SECRET" in item for item in main._runtime_config_errors())


@pytest.mark.asyncio
async def test_health_ready_metrics_and_request_id_logging(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEFAULT_ORG_ID", "1")
    monkeypatch.setenv("USER_AUTH_SECRET", "super-secret")
    monkeypatch.setattr(main.settings, "tenant_config_strict", False)
    monkeypatch.setattr(main.settings, "whatsapp_verify_token", "")
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", "")
    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession()))

    captured = {"request_ids": []}

    def fake_info(message, *args, **kwargs):
        if message == "HTTP request completed":
            captured["request_ids"].append(main.request_id_ctx.get("-"))

    monkeypatch.setattr(main.logger, "info", fake_info)

    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        health = await client.get("/healthz", headers={"x-request-id": "req-123"})
        ready = await client.get("/readyz")
        metrics = await client.get("/metrics")

    assert health.status_code == 200
    assert health.headers["x-request-id"] == "req-123"
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"] == "ok"
    assert metrics.status_code == 200
    assert "bot_http_requests_total" in metrics.text
    assert "req-123" in captured["request_ids"]


@pytest.mark.asyncio
async def test_readyz_handles_config_and_database_failures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEFAULT_ORG_ID", "bad")
    monkeypatch.setenv("USER_AUTH_SECRET", "change-me")
    monkeypatch.setattr(main.settings, "tenant_config_strict", False)
    monkeypatch.setattr(main.settings, "whatsapp_verify_token", "")
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", "")
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        config_failed = await client.get("/readyz")
    assert config_failed.status_code == 503
    assert config_failed.json()["checks"]["config"] == "failed"

    monkeypatch.setenv("DEFAULT_ORG_ID", "1")
    monkeypatch.setenv("USER_AUTH_SECRET", "super-secret")
    monkeypatch.setattr(main.settings, "tenant_config_strict", False)
    monkeypatch.setattr(main.settings, "whatsapp_verify_token", "")
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", "")
    monkeypatch.setattr(main, "AsyncSessionLocal", lambda: _FakeSessionManager(_FakeSession(should_fail=True)))
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        db_failed = await client.get("/readyz")
    assert db_failed.status_code == 503
    assert db_failed.json()["checks"]["database"] == "failed"


@pytest.mark.asyncio
async def test_lifespan_strict_startup_raises_before_init_db(monkeypatch: pytest.MonkeyPatch):
    init_db_called: list[str] = []

    async def fake_init_db():
        init_db_called.append("init_db")

    monkeypatch.setenv("DEFAULT_ORG_ID", "1")
    monkeypatch.setenv("USER_AUTH_SECRET", "change-me")
    monkeypatch.setattr(main.settings, "strict_startup_validation", True)
    monkeypatch.setattr(main.settings, "tenant_config_strict", False)
    monkeypatch.setattr(main.settings, "whatsapp_verify_token", "")
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", "")
    monkeypatch.setattr(main, "init_db", fake_init_db)

    with pytest.raises(RuntimeError, match="USER_AUTH_SECRET"):
        async with main.lifespan(main.app):
            pass

    assert init_db_called == []


@pytest.mark.asyncio
async def test_lifespan_strict_startup_requires_tenant_secrets_master_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DEFAULT_ORG_ID", "1")
    monkeypatch.setenv("USER_AUTH_SECRET", "super-secret")
    monkeypatch.setattr(main.settings, "strict_startup_validation", True)
    monkeypatch.setattr(main.settings, "tenant_config_strict", True)
    monkeypatch.setattr(main.settings, "tenant_secrets_master_key", "")
    monkeypatch.setattr(main.settings, "whatsapp_verify_token", "")
    monkeypatch.setattr(main.settings, "whatsapp_app_secret", "")

    with pytest.raises(RuntimeError, match="TENANT_SECRETS_MASTER_KEY"):
        async with main.lifespan(main.app):
            pass

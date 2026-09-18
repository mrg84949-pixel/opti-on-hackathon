from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import bot.notifications.admin_telegram as admin_telegram


class _FakeScalarsAllResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


class _FakeSession:
    def __init__(self, *, org=None, customer=None, admins=None):
        self._org = org
        self._customer = customer
        self._admins = admins or []

    async def get(self, model, key):
        model_name = getattr(model, "__name__", "")
        if model_name == "Organization":
            return self._org
        if model_name == "Customer":
            return self._customer
        return None

    async def execute(self, _stmt):
        return _FakeScalarsAllResult(self._admins)


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeResponse:
    def __init__(self, *, status_code: int = 200):
        self.status_code = status_code

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return self.response


def test_admin_bot_url_and_send(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ADMIN_TELEGRAM_BOT_TOKEN", raising=False)
    assert admin_telegram._admin_bot_url() is None

    monkeypatch.setenv("ADMIN_TELEGRAM_BOT_TOKEN", "token")
    assert admin_telegram._admin_bot_url() == "https://api.telegram.org/bottoken/sendMessage"


@pytest.mark.asyncio
async def test_admin_send_success_failure_and_missing_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_BOT_TOKEN", "token")
    ok_client = _FakeClient(_FakeResponse(status_code=200))
    monkeypatch.setattr(admin_telegram.httpx, "AsyncClient", lambda timeout=20: ok_client)
    assert await admin_telegram._send(111, "hello") is True
    assert ok_client.calls[0]["json"]["chat_id"] == 111

    fail_client = _FakeClient(_FakeResponse(status_code=500))
    monkeypatch.setattr(admin_telegram.httpx, "AsyncClient", lambda timeout=20: fail_client)
    assert await admin_telegram._send(111, "hello") is False

    monkeypatch.delenv("ADMIN_TELEGRAM_BOT_TOKEN", raising=False)
    assert await admin_telegram._send(111, "hello") is False


@pytest.mark.asyncio
async def test_notify_admins_about_new_appointment_no_admins_partial_success_and_exception(
    monkeypatch: pytest.MonkeyPatch,
):
    org = SimpleNamespace(id=1, name="Acme")
    customer = SimpleNamespace(id=2, name="Aruzhan", phone="wa:777")

    no_admins_session = _FakeSession(org=org, customer=customer, admins=[])
    monkeypatch.setattr(admin_telegram, "AsyncSessionLocal", lambda: _FakeSessionManager(no_admins_session))
    sent_zero = await admin_telegram.notify_admins_about_new_appointment(
        org_id=1,
        customer_id=2,
        when_local=datetime(2026, 5, 1, 10, 0),
        timezone_name="UTC",
        local_appointment_id=10,
    )
    assert sent_zero == 0

    admins = [SimpleNamespace(telegram_id=111), SimpleNamespace(telegram_id=222), SimpleNamespace(telegram_id=333)]
    partial_session = _FakeSession(org=org, customer=customer, admins=admins)
    monkeypatch.setattr(admin_telegram, "AsyncSessionLocal", lambda: _FakeSessionManager(partial_session))
    calls = {"count": 0}

    async def fake_send(chat_id: int, text: str):
        calls["count"] += 1
        assert "Организация: Acme" in text
        if chat_id == 111:
            return True
        if chat_id == 222:
            return False
        raise RuntimeError("network")

    monkeypatch.setattr(admin_telegram, "_send", fake_send)
    sent_partial = await admin_telegram.notify_admins_about_new_appointment(
        org_id=1,
        customer_id=2,
        when_local=datetime(2026, 5, 1, 10, 0),
        timezone_name="UTC",
        local_appointment_id=10,
        crm_appointment_id="crm-1",
    )
    assert sent_partial == 1
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_notify_admins_about_human_transfer_missing_entities_and_partial_success(
    monkeypatch: pytest.MonkeyPatch,
):
    missing_session = _FakeSession(org=None, customer=None, admins=[])
    monkeypatch.setattr(admin_telegram, "AsyncSessionLocal", lambda: _FakeSessionManager(missing_session))
    assert await admin_telegram.notify_admins_about_human_transfer(org_id=1, customer_id=2) == 0

    org = SimpleNamespace(id=1, name="Acme")
    customer = SimpleNamespace(id=2, name="Aruzhan", phone="wa:777")
    admins = [SimpleNamespace(telegram_id=111), SimpleNamespace(telegram_id=222)]
    full_session = _FakeSession(org=org, customer=customer, admins=admins)
    monkeypatch.setattr(admin_telegram, "AsyncSessionLocal", lambda: _FakeSessionManager(full_session))

    async def fake_send(chat_id: int, text: str):
        assert "Организация: Acme" in text
        return chat_id == 111

    monkeypatch.setattr(admin_telegram, "_send", fake_send)
    assert await admin_telegram.notify_admins_about_human_transfer(org_id=1, customer_id=2) == 1

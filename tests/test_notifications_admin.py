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


@pytest.mark.asyncio
async def test_notify_admins_about_new_appointment_returns_zero_for_missing_entities(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_session = _FakeSession(org=None, customer=None, admins=[])
    monkeypatch.setattr(admin_telegram, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))
    sent = await admin_telegram.notify_admins_about_new_appointment(
        org_id=1,
        customer_id=1,
        when_local=datetime(2026, 5, 1, 10, 0),
        timezone_name="UTC",
        local_appointment_id=1,
    )
    assert sent == 0


@pytest.mark.asyncio
async def test_notify_admins_about_human_transfer_sends_to_all_admins(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(id=1, name="Clinic")
    customer = SimpleNamespace(id=2, name="Aruzhan", phone="wa:777")
    admins = [SimpleNamespace(telegram_id=111), SimpleNamespace(telegram_id=222)]
    fake_session = _FakeSession(org=org, customer=customer, admins=admins)
    monkeypatch.setattr(admin_telegram, "AsyncSessionLocal", lambda: _FakeSessionManager(fake_session))

    calls = {"count": 0}

    async def _send(chat_id: int, text: str):
        calls["count"] += 1
        assert "Требуется подключение администратора" in text
        assert "Бот замьючен на 7 дней" in text
        return True

    monkeypatch.setattr(admin_telegram, "_send", _send)
    sent = await admin_telegram.notify_admins_about_human_transfer(org_id=1, customer_id=2)
    assert sent == 2
    assert calls["count"] == 2


from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.automation import client_change_timeouts
from bot.db.models import AppointmentStatus
from bot.services.outbound_result import OutboundSendResult


class _ScalarsAll:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _ScalarsAll(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.committed = False

    async def execute(self, _stmt):
        return _ExecuteResult(self.rows)

    async def commit(self):
        self.committed = True


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _expired_appt():
    org = SimpleNamespace(
        id=1,
        timezone="UTC",
        bot_enabled=True,
        billing_paid_until=None,
    )
    customer = SimpleNamespace(id=2, phone="tg:9", organization=org)
    appt = SimpleNamespace(
        id=7,
        scheduled_at=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc),
        status=AppointmentStatus.CONFIRMED,
        client_change_deadline_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        customer=customer,
    )
    return appt


@pytest.mark.asyncio
async def test_process_client_change_timeouts_cancels_on_send(monkeypatch: pytest.MonkeyPatch):
    appt = _expired_appt()
    fake_session = _FakeSession([appt])
    cancelled = {"called": False}

    async def fake_send(org, customer, text):
        assert "24 часов" in text
        return OutboundSendResult.success()

    async def fake_cancel(session, org_id, appt_id, reason):
        cancelled["called"] = True
        assert org_id == 1
        assert appt_id == 7
        assert "24 часов" in reason

    monkeypatch.setattr(
        client_change_timeouts,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(fake_session),
    )
    monkeypatch.setattr(
        client_change_timeouts.notification_service,
        "send_customer_message",
        fake_send,
    )
    monkeypatch.setattr(
        client_change_timeouts.appointment_service,
        "cancel_appointment",
        fake_cancel,
    )

    await client_change_timeouts.process_client_change_timeouts()
    assert cancelled["called"] is True
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_process_client_change_timeouts_skips_when_bot_paused(monkeypatch: pytest.MonkeyPatch):
    appt = _expired_appt()
    appt.customer.organization = SimpleNamespace(
        id=1,
        timezone="UTC",
        bot_enabled=False,
        billing_paid_until=None,
    )
    fake_session = _FakeSession([appt])

    async def fake_send(org, customer, text):
        raise AssertionError("send should not run for paused org")

    async def fake_cancel(session, org_id, appt_id, reason):
        raise AssertionError("cancel should not run for paused org")

    monkeypatch.setattr(
        client_change_timeouts,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(fake_session),
    )
    monkeypatch.setattr(
        client_change_timeouts.notification_service,
        "send_customer_message",
        fake_send,
    )
    monkeypatch.setattr(
        client_change_timeouts.appointment_service,
        "cancel_appointment",
        fake_cancel,
    )

    await client_change_timeouts.process_client_change_timeouts()
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_process_client_change_timeouts_skips_when_billing_expired(monkeypatch: pytest.MonkeyPatch):
    appt = _expired_appt()
    appt.customer.organization = SimpleNamespace(
        id=1,
        timezone="UTC",
        bot_enabled=True,
        billing_paid_until=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    fake_session = _FakeSession([appt])

    async def fake_send(org, customer, text):
        raise AssertionError("send should not run for expired billing org")

    async def fake_cancel(session, org_id, appt_id, reason):
        raise AssertionError("cancel should not run for expired billing org")

    monkeypatch.setattr(
        client_change_timeouts,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(fake_session),
    )
    monkeypatch.setattr(
        client_change_timeouts.notification_service,
        "send_customer_message",
        fake_send,
    )
    monkeypatch.setattr(
        client_change_timeouts.appointment_service,
        "cancel_appointment",
        fake_cancel,
    )

    await client_change_timeouts.process_client_change_timeouts()
    assert fake_session.committed is True


@pytest.mark.asyncio
async def test_process_client_change_timeouts_skips_when_send_fails(monkeypatch: pytest.MonkeyPatch):
    appt = _expired_appt()
    fake_session = _FakeSession([appt])

    async def fake_send(org, customer, text):
        return OutboundSendResult.retryable_error()

    async def fake_cancel(session, org_id, appt_id, reason):
        raise AssertionError("cancel should not run when send fails")

    monkeypatch.setattr(
        client_change_timeouts,
        "AsyncSessionLocal",
        lambda: _FakeSessionManager(fake_session),
    )
    monkeypatch.setattr(
        client_change_timeouts.notification_service,
        "send_customer_message",
        fake_send,
    )
    monkeypatch.setattr(
        client_change_timeouts.appointment_service,
        "cancel_appointment",
        fake_cancel,
    )

    await client_change_timeouts.process_client_change_timeouts()
    assert fake_session.committed is True

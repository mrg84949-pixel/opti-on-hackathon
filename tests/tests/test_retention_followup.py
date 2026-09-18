from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.automation import retention
from bot.db.models import AppointmentStatus
from bot.services import appointment_service, notification_service
from bot.services.appointment_service import AppointmentNotFoundError
from bot.services.outbound_result import OutboundSendResult


def _org(**overrides):
    base = {
        "id": 1,
        "name": "Smile Clinic",
        "bot_enabled": True,
        "billing_paid_until": None,
        "retention_days_after_complete": 14,
        "retention_message": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_complete_appointment_sets_completed_at():
    appt = SimpleNamespace(
        id=10,
        status=AppointmentStatus.CONFIRMED,
        scheduled_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        completed_at=None,
        customer_id=5,
        crm_appointment_id=None,
        crm_doctor_id=None,
        reminder_24h_sent_at=None,
        reminder_2h_sent_at=None,
    )
    customer = SimpleNamespace(
        id=5,
        name="Ali",
        phone="tg:123",
        org_id=1,
        organization=SimpleNamespace(id=1, name="Clinic", timezone="UTC"),
        dialog_context={"dialog_mode": "manage", "last_appointment_id": 10},
    )

    class _FakeSession:
        async def flush(self):
            return None

    session = _FakeSession()

    async def fake_get(session, org_id, appt_id):
        if appt_id == 10:
            return appt, customer
        raise AppointmentNotFoundError()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(appointment_service, "_get_appointment_with_customer", fake_get)
    try:
        before = datetime.now(timezone.utc)
        item, _ = await appointment_service.complete_appointment(session, 1, 10)
        after = datetime.now(timezone.utc)
        assert item["status"] == "completed"
        assert appt.completed_at is not None
        assert before <= appt.completed_at <= after
    finally:
        monkeypatch.undo()


def test_render_retention_text_default():
    org = _org(name="Smile Clinic")
    customer = SimpleNamespace(id=1)
    text = notification_service.render_retention_text(org, customer)
    assert "Smile Clinic" in text


def test_render_retention_text_custom():
    org = _org(retention_message="Привет из {org_name}!")
    customer = SimpleNamespace(id=1)
    text = notification_service.render_retention_text(org, customer)
    assert text == "Привет из Smile Clinic!"


@pytest.mark.asyncio
async def test_process_retention_followups_sends_and_marks(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    days = 14
    completed_at = now - timedelta(days=days) + timedelta(minutes=2)
    ok_org = _org(retention_days_after_complete=days)
    ok_customer = SimpleNamespace(
        id=10,
        phone="wa:777@c.us",
        organization=ok_org,
        muted_until=None,
        disable_reminders=False,
    )
    ok_appt = SimpleNamespace(
        id=100,
        status=AppointmentStatus.COMPLETED,
        completed_at=completed_at,
        retention_sent_at=None,
        customer=ok_customer,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [ok_appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    fake_session = _FakeSession()

    class _FakeSessionManager:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent: list[str] = []

    async def fake_send(org_arg, customer_arg, text_arg):
        sent.append(text_arg)
        return OutboundSendResult.success()

    monkeypatch.setattr(retention, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await retention.process_retention_followups()

    assert fake_session.committed is True
    assert ok_appt.retention_sent_at is not None
    assert len(sent) == 1
    assert "Smile Clinic" in sent[0]


@pytest.mark.asyncio
async def test_process_retention_skips_disabled_org_days(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    org = _org(retention_days_after_complete=0)
    customer = SimpleNamespace(
        id=10,
        phone="wa:777@c.us",
        organization=org,
        muted_until=None,
        disable_reminders=False,
    )
    appt = SimpleNamespace(
        id=100,
        status=AppointmentStatus.COMPLETED,
        completed_at=now - timedelta(days=14),
        retention_sent_at=None,
        customer=customer,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [appt]

    class _FakeSession:
        committed = False

        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            self.committed = True

    class _FakeSessionManager:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent: list[str] = []

    async def fake_send(org_arg, customer_arg, text_arg):
        sent.append(text_arg)
        return OutboundSendResult.success()

    monkeypatch.setattr(retention, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await retention.process_retention_followups()

    assert appt.retention_sent_at is None
    assert sent == []


@pytest.mark.asyncio
async def test_process_retention_skips_reminder_opt_out(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    org = _org(retention_days_after_complete=14)
    customer = SimpleNamespace(
        id=10,
        phone="wa:777@c.us",
        organization=org,
        muted_until=None,
        disable_reminders=True,
    )
    appt = SimpleNamespace(
        id=100,
        status=AppointmentStatus.COMPLETED,
        completed_at=now - timedelta(days=14) + timedelta(minutes=2),
        retention_sent_at=None,
        customer=customer,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [appt]

    class _FakeSession:
        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            return None

    class _FakeSessionManager:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent: list[str] = []

    async def fake_send(org_arg, customer_arg, text_arg):
        sent.append(text_arg)
        return OutboundSendResult.success()

    monkeypatch.setattr(retention, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await retention.process_retention_followups()

    assert appt.retention_sent_at is None
    assert sent == []


@pytest.mark.asyncio
async def test_process_retention_skips_frozen_org(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    org = _org(retention_days_after_complete=14, bot_enabled=False)
    customer = SimpleNamespace(
        id=10,
        phone="wa:777@c.us",
        organization=org,
        muted_until=None,
        disable_reminders=False,
    )
    appt = SimpleNamespace(
        id=100,
        status=AppointmentStatus.COMPLETED,
        completed_at=now - timedelta(days=14) + timedelta(minutes=2),
        retention_sent_at=None,
        customer=customer,
    )

    class _FakeResult:
        def scalars(self):
            return self

        def all(self):
            return [appt]

    class _FakeSession:
        async def execute(self, _):
            return _FakeResult()

        async def commit(self):
            return None

    class _FakeSessionManager:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    sent: list[str] = []

    async def fake_send(org_arg, customer_arg, text_arg):
        sent.append(text_arg)
        return OutboundSendResult.success()

    monkeypatch.setattr(retention, "AsyncSessionLocal", lambda: _FakeSessionManager())
    monkeypatch.setattr(notification_service, "send_customer_message", fake_send)
    await retention.process_retention_followups()

    assert appt.retention_sent_at is None
    assert sent == []

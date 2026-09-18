from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.db.models import AppointmentStatus
from bot.services import appointment_service
from bot.services.client_change_intent import parse_client_change_intent


class _OneRowResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _ScalarConflictResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ProposeSession:
    def __init__(self, row, *, conflict=None):
        self.row = row
        self.conflict = conflict
        self._calls = 0
        self.flushed = False

    async def execute(self, _stmt):
        self._calls += 1
        if self._calls == 1:
            return _OneRowResult(self.row)
        return _ScalarConflictResult(self.conflict)

    async def flush(self):
        self.flushed = True


def _future_propose_date(*, days_ahead: int = 30) -> str:
    """ISO date safely in the future for propose_appointment_change tests."""
    return (datetime.now(timezone.utc).date() + timedelta(days=days_ahead)).isoformat()


def _active_appt(*, status=AppointmentStatus.CONFIRMED, deadline=None):
    scheduled = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    org = SimpleNamespace(id=1, name="Org", timezone="UTC")
    customer = SimpleNamespace(id=5, name="Ali", phone="tg:1", org_id=1, organization=org)
    appt = SimpleNamespace(
        id=10,
        customer_id=5,
        scheduled_at=scheduled,
        status=status,
        crm_appointment_id=None,
        crm_doctor_id="doc-1",
        reminder_24h_sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reminder_2h_sent_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        cancel_reason=None,
        client_change_requested_at=None,
        client_change_deadline_at=deadline,
        client_change_reason=None,
    )
    return appt, customer


def test_parse_client_change_intent():
    assert parse_client_change_intent("Да") is True
    assert parse_client_change_intent("нет") is False
    assert parse_client_change_intent("maybe") is None


@pytest.mark.asyncio
async def test_propose_appointment_change_sets_deadline():
    appt, customer = _active_appt()
    session = _ProposeSession((appt, customer))
    item, _, _ = await appointment_service.propose_appointment_change(
        session,
        1,
        10,
        date=_future_propose_date(),
        time="15:00",
        reason="Перенос по расписанию",
        tz_name="UTC",
    )
    assert appt.client_change_reason == "Перенос по расписанию"
    assert appt.client_change_requested_at is not None
    assert appt.client_change_deadline_at is not None
    delta = appt.client_change_deadline_at - appt.client_change_requested_at
    assert delta == timedelta(hours=appointment_service.settings.client_change_response_hours)
    assert appt.reminder_24h_sent_at is None
    assert item["client_change_reason"] == "Перенос по расписанию"


@pytest.mark.asyncio
async def test_propose_appointment_change_updates_doctor_id():
    appt, customer = _active_appt()
    session = _ProposeSession((appt, customer))
    item, _, _ = await appointment_service.propose_appointment_change(
        session,
        1,
        10,
        date=_future_propose_date(),
        time="15:00",
        reason="Смена специалиста",
        doctor_id="doc-2",
        tz_name="UTC",
    )
    assert appt.crm_doctor_id == "doc-2"
    assert item["crm_doctor_id"] == "doc-2"


@pytest.mark.asyncio
async def test_propose_rejects_when_pending():
    deadline = datetime(2026, 6, 10, tzinfo=timezone.utc)
    appt, customer = _active_appt(deadline=deadline)
    session = _ProposeSession((appt, customer))
    with pytest.raises(appointment_service.ClientChangePendingError):
        await appointment_service.propose_appointment_change(
            session,
            1,
            10,
            date=_future_propose_date(),
            time="15:00",
            reason="Ещё раз",
            tz_name="UTC",
        )


@pytest.mark.asyncio
async def test_accept_clears_flags_and_sets_new(monkeypatch: pytest.MonkeyPatch):
    deadline = datetime(2026, 12, 1, tzinfo=timezone.utc)
    appt, customer = _active_appt(deadline=deadline)
    session = _ProposeSession((appt, customer))

    async def fake_get_awaiting(_session, org_id, customer_id):
        return appt, customer

    monkeypatch.setattr(
        appointment_service,
        "get_appointment_awaiting_client_change",
        fake_get_awaiting,
    )
    item, _ = await appointment_service.accept_appointment_change(session, 1, 5)
    assert item["status"] == "new"
    assert appt.status == AppointmentStatus.NEW
    assert appt.client_change_deadline_at is None


@pytest.mark.asyncio
async def test_reject_cancels_appointment(monkeypatch: pytest.MonkeyPatch):
    deadline = datetime(2026, 12, 1, tzinfo=timezone.utc)
    appt, customer = _active_appt(status=AppointmentStatus.NEW, deadline=deadline)
    session = _ProposeSession((appt, customer))

    async def fake_get_awaiting(_session, org_id, customer_id):
        return appt, customer

    monkeypatch.setattr(
        appointment_service,
        "get_appointment_awaiting_client_change",
        fake_get_awaiting,
    )
    item, _ = await appointment_service.reject_appointment_change(session, 1, 5)
    assert item["status"] == "cancelled"
    assert appt.client_change_deadline_at is None

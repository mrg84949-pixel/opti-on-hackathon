from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.db.models import AppointmentStatus
from bot.services import appointment_service


def test_validate_cancel_reason():
    assert appointment_service._validate_cancel_reason("  ok reason  ") == "ok reason"
    with pytest.raises(appointment_service.InvalidCancelReasonError):
        appointment_service._validate_cancel_reason("ab")
    with pytest.raises(appointment_service.InvalidCancelReasonError):
        appointment_service._validate_cancel_reason("x" * 501)


def test_status_transition_matrix():
    appointment_service._assert_transition(AppointmentStatus.NEW, AppointmentStatus.CONFIRMED)
    appointment_service._assert_transition(AppointmentStatus.NEW, AppointmentStatus.CANCELLED)
    appointment_service._assert_transition(AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED)
    appointment_service._assert_transition(AppointmentStatus.CONFIRMED, AppointmentStatus.CANCELLED)

    with pytest.raises(appointment_service.InvalidStatusTransitionError):
        appointment_service._assert_transition(AppointmentStatus.NEW, AppointmentStatus.COMPLETED)
    with pytest.raises(appointment_service.InvalidStatusTransitionError):
        appointment_service._assert_transition(AppointmentStatus.CANCELLED, AppointmentStatus.CONFIRMED)
    with pytest.raises(appointment_service.InvalidStatusTransitionError):
        appointment_service._assert_transition(AppointmentStatus.COMPLETED, AppointmentStatus.CONFIRMED)


class _OneRowResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self.row = row
        self.flushed = False

    async def execute(self, _stmt):
        return _OneRowResult(self.row)

    async def flush(self):
        self.flushed = True


def _appointment_row(*, status: AppointmentStatus, cancel_reason: str | None = None):
    scheduled = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    org = SimpleNamespace(id=1, name="Org", timezone="UTC")
    customer = SimpleNamespace(
        id=5,
        name="Ali",
        phone="tg:12345",
        org_id=1,
        organization=org,
    )
    appt = SimpleNamespace(
        id=10,
        customer_id=5,
        scheduled_at=scheduled,
        status=status,
        crm_appointment_id=None,
        crm_doctor_id=None,
        reminder_24h_sent_at=None,
        cancel_reason=cancel_reason,
    )
    return appt, customer


@pytest.mark.asyncio
async def test_confirm_appointment_success():
    appt, customer = _appointment_row(status=AppointmentStatus.NEW)
    session = _FakeSession((appt, customer))
    item, returned_customer = await appointment_service.confirm_appointment(session, 1, 10)
    assert item["status"] == "confirmed"
    assert appt.status == AppointmentStatus.CONFIRMED
    assert returned_customer is customer
    assert session.flushed is True


@pytest.mark.asyncio
async def test_cancel_appointment_sets_reason():
    appt, customer = _appointment_row(status=AppointmentStatus.CONFIRMED)
    session = _FakeSession((appt, customer))
    item, _ = await appointment_service.cancel_appointment(session, 1, 10, "Клиент перенёс визит")
    assert item["status"] == "cancelled"
    assert appt.cancel_reason == "Клиент перенёс визит"
    assert item["cancel_reason"] == "Клиент перенёс визит"


@pytest.mark.asyncio
async def test_complete_appointment_success():
    appt, customer = _appointment_row(status=AppointmentStatus.CONFIRMED)
    customer.dialog_context = {
        "dialog_mode": "manage",
        "last_appointment_id": 10,
        "pending_booking": {"service": "X"},
        "context_summary": "old summary text that should be replaced after complete",
    }
    session = _FakeSession((appt, customer))
    item, _ = await appointment_service.complete_appointment(session, 1, 10)
    assert item["status"] == "completed"
    assert appt.status == AppointmentStatus.COMPLETED
    assert appt.completed_at is not None
    assert customer.dialog_context.get("dialog_mode") == "booking"
    assert "last_appointment_id" not in customer.dialog_context
    assert "pending_booking" not in customer.dialog_context
    summary = customer.dialog_context.get("context_summary", "")
    assert "Запись №10" in summary
    assert "old summary" not in summary


@pytest.mark.asyncio
async def test_get_appointment_not_found():
    session = _FakeSession(None)
    with pytest.raises(appointment_service.AppointmentNotFoundError):
        await appointment_service.confirm_appointment(session, 1, 999)


@pytest.mark.asyncio
async def test_confirm_invalid_transition():
    appt, customer = _appointment_row(status=AppointmentStatus.CANCELLED)
    session = _FakeSession((appt, customer))
    with pytest.raises(appointment_service.InvalidStatusTransitionError):
        await appointment_service.confirm_appointment(session, 1, 10)


class _ScalarConflictResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RescheduleSession(_FakeSession):
    def __init__(self, row, *, conflict=None):
        super().__init__(row)
        self.conflict = conflict
        self._calls = 0

    async def execute(self, _stmt):
        self._calls += 1
        if self._calls == 1:
            return _OneRowResult(self.row)
        return _ScalarConflictResult(self.conflict)


@pytest.mark.asyncio
async def test_get_active_appointment_for_customer():
    appt, customer = _appointment_row(status=AppointmentStatus.CONFIRMED)
    session = _FakeSession((appt, customer))
    found = await appointment_service.get_active_appointment_for_customer(session, 1, 5)
    assert found is not None
    assert found[0] is appt


@pytest.mark.asyncio
async def test_get_active_appointment_none():
    session = _FakeSession(None)
    assert await appointment_service.get_active_appointment_for_customer(session, 1, 5) is None


@pytest.mark.asyncio
async def test_cancel_appointment_by_customer_defaults_reason(monkeypatch: pytest.MonkeyPatch):
    appt, customer = _appointment_row(status=AppointmentStatus.NEW)
    session = _FakeSession((appt, customer))

    async def fake_get_active(_session, org_id, customer_id):
        return appt, customer

    monkeypatch.setattr(
        appointment_service,
        "get_active_appointment_for_customer",
        fake_get_active,
    )
    item, _ = await appointment_service.cancel_appointment_by_customer(session, 1, 5, "")
    assert item["status"] == "cancelled"
    assert appt.cancel_reason == appointment_service.DEFAULT_CLIENT_CANCEL_REASON


@pytest.mark.asyncio
async def test_reschedule_appointment_success():
    appt, customer = _appointment_row(status=AppointmentStatus.CONFIRMED)
    appt.crm_doctor_id = "doc-1"
    session = _RescheduleSession((appt, customer), conflict=None)
    item, _, local_dt = await appointment_service.reschedule_appointment(
        session, 1, 10, date="2027-06-02", time="14:00", tz_name="UTC"
    )
    assert item["status"] == "new"
    assert appt.status == AppointmentStatus.NEW
    assert local_dt.hour == 14


@pytest.mark.asyncio
async def test_reschedule_slot_conflict():
    appt, customer = _appointment_row(status=AppointmentStatus.NEW)
    appt.crm_doctor_id = "doc-1"
    session = _RescheduleSession((appt, customer), conflict=SimpleNamespace(id=99))
    with pytest.raises(ValueError, match="уже занято"):
        await appointment_service.reschedule_appointment(
            session, 1, 10, date="2027-06-02", time="14:00", tz_name="UTC"
        )


@pytest.mark.asyncio
async def test_reschedule_invalid_status():
    appt, customer = _appointment_row(status=AppointmentStatus.COMPLETED)
    session = _FakeSession((appt, customer))
    with pytest.raises(appointment_service.InvalidStatusTransitionError):
        await appointment_service.reschedule_appointment(
            session, 1, 10, date="2027-06-02", time="14:00", tz_name="UTC"
        )

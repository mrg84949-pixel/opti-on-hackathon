from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.crm.base import Slot
from bot.db.models import AppointmentStatus
from bot.services import appointment_service


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def _extract_where_values(stmt):
    values: dict[str, object] = {}

    def walk(expr):
        if expr is None:
            return
        clauses = getattr(expr, "clauses", None)
        if clauses is not None:
            for child in clauses:
                walk(child)
            return
        left = getattr(expr, "left", None)
        right = getattr(expr, "right", None)
        key = getattr(left, "key", None)
        if key and right is not None:
            values[key] = getattr(right, "value", right)

    walk(stmt.whereclause)
    return values


class _ConflictLookupSession:
    def __init__(self, rows: list[tuple[object, object]]):
        self.rows = rows

    async def execute(self, stmt):
        criteria = _extract_where_values(stmt)
        org_id = criteria.get("org_id")
        doctor_id = criteria.get("crm_doctor_id")
        scheduled_at = criteria.get("scheduled_at")

        for appt, customer in self.rows:
            if org_id is not None and customer.org_id != org_id:
                continue
            if doctor_id is not None and appt.crm_doctor_id != doctor_id:
                continue
            if scheduled_at is not None and appt.scheduled_at != scheduled_at:
                continue
            if appt.status not in (AppointmentStatus.NEW, AppointmentStatus.CONFIRMED):
                continue
            return _ScalarOneOrNoneResult(appt)
        return _ScalarOneOrNoneResult(None)


class _TrackingProvider:
    def __init__(self, slots: list[Slot]):
        self.slots = slots
        self.slots_called = False

    async def get_available_slots(self, doctor_id: str, date_iso: str, *, tz_name: str | None = None):
        self.slots_called = True
        return self.slots


def _org(**overrides):
    base = {"id": 1, "timezone": "UTC"}
    base.update(overrides)
    return SimpleNamespace(**base)


def _slot_at(hour: int, minute: int = 0) -> Slot:
    start = datetime(2027, 6, 15, hour, minute, tzinfo=timezone.utc)
    return Slot(start=start, end=start)


@pytest.mark.asyncio
async def test_validate_raises_on_local_conflict_without_crm_call():
    utc_dt = datetime(2027, 6, 15, 10, 0, tzinfo=timezone.utc)
    customer = SimpleNamespace(id=10, org_id=1)
    appt = SimpleNamespace(
        id=1,
        crm_doctor_id="doc-1",
        scheduled_at=utc_dt,
        status=AppointmentStatus.NEW,
        customer_id=customer.id,
    )
    session = _ConflictLookupSession([(appt, customer)])
    provider = _TrackingProvider(slots=[_slot_at(10)])
    local_dt = datetime(2027, 6, 15, 10, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="уже занято"):
        await appointment_service.validate_booking_slot_available(
            session,
            org=_org(),
            doctor_id="doc-1",
            local_dt=local_dt,
            utc_dt=utc_dt,
            provider=provider,
        )

    assert provider.slots_called is False


@pytest.mark.asyncio
async def test_validate_raises_when_crm_slots_missing_time():
    utc_dt = datetime(2027, 6, 15, 10, 0, tzinfo=timezone.utc)
    session = _ConflictLookupSession([])
    provider = _TrackingProvider(slots=[_slot_at(11)])
    local_dt = datetime(2027, 6, 15, 10, 0, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="уже занято"):
        await appointment_service.validate_booking_slot_available(
            session,
            org=_org(),
            doctor_id="doc-1",
            local_dt=local_dt,
            utc_dt=utc_dt,
            provider=provider,
        )

    assert provider.slots_called is True


@pytest.mark.asyncio
async def test_validate_passes_when_local_and_crm_ok():
    utc_dt = datetime(2027, 6, 15, 10, 0, tzinfo=timezone.utc)
    session = _ConflictLookupSession([])
    provider = _TrackingProvider(slots=[_slot_at(10)])
    local_dt = datetime(2027, 6, 15, 10, 0, tzinfo=timezone.utc)

    await appointment_service.validate_booking_slot_available(
        session,
        org=_org(),
        doctor_id="doc-1",
        local_dt=local_dt,
        utc_dt=utc_dt,
        provider=provider,
    )

    assert provider.slots_called is True

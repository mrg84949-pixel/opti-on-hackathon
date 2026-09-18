from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

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
        exclude_id = criteria.get("id")

        for appt, customer in self.rows:
            if org_id is not None and customer.org_id != org_id:
                continue
            if doctor_id is not None and appt.crm_doctor_id != doctor_id:
                continue
            if scheduled_at is not None and appt.scheduled_at != scheduled_at:
                continue
            if exclude_id is not None and appt.id == exclude_id:
                continue
            if appt.status not in (AppointmentStatus.NEW, AppointmentStatus.CONFIRMED):
                continue
            return _ScalarOneOrNoneResult(appt)
        return _ScalarOneOrNoneResult(None)


def _row(*, org_id: int, appt_id: int, doctor_id: str, scheduled_at: datetime):
    customer = SimpleNamespace(id=appt_id * 10, org_id=org_id)
    appt = SimpleNamespace(
        id=appt_id,
        crm_doctor_id=doctor_id,
        scheduled_at=scheduled_at,
        status=AppointmentStatus.NEW,
        customer_id=customer.id,
    )
    return appt, customer


@pytest.mark.asyncio
async def test_find_slot_conflict_same_org_blocks():
    slot = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    session = _ConflictLookupSession([_row(org_id=1, appt_id=1, doctor_id="doc-1", scheduled_at=slot)])

    conflict = await appointment_service._find_slot_conflict(
        session,
        org_id=1,
        doctor_id="doc-1",
        utc_dt=slot,
    )

    assert conflict is not None
    assert conflict.id == 1


@pytest.mark.asyncio
async def test_find_slot_conflict_other_org_ignored():
    slot = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    session = _ConflictLookupSession([_row(org_id=1, appt_id=1, doctor_id="doc-1", scheduled_at=slot)])

    conflict = await appointment_service._find_slot_conflict(
        session,
        org_id=2,
        doctor_id="doc-1",
        utc_dt=slot,
    )

    assert conflict is None


@pytest.mark.asyncio
async def test_find_slot_conflict_honors_exclude_appt_id():
    slot = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
    session = _ConflictLookupSession([_row(org_id=1, appt_id=5, doctor_id="doc-1", scheduled_at=slot)])

    conflict = await appointment_service._find_slot_conflict(
        session,
        org_id=1,
        doctor_id="doc-1",
        utc_dt=slot,
        exclude_appt_id=5,
    )

    assert conflict is None

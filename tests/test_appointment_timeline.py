from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.db.models import AppointmentStatus
from bot.services import appointment_service


class _AllRowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _AllRowsResult(self._rows)


def _row(*, doctor_id: str | None, hour: int, minute: int, appt_id: int):
    scheduled = datetime(2026, 6, 2, hour, minute, tzinfo=timezone.utc)
    org = SimpleNamespace(id=1)
    customer = SimpleNamespace(
        id=5,
        name="Ali",
        phone="tg:1",
        org_id=1,
        organization=org,
    )
    appt = SimpleNamespace(
        id=appt_id,
        customer_id=5,
        scheduled_at=scheduled,
        status=AppointmentStatus.NEW,
        crm_appointment_id=None,
        crm_doctor_id=doctor_id,
        reminder_24h_sent_at=None,
        reminder_2h_sent_at=None,
        cancel_reason=None,
    )
    return appt, customer


@pytest.mark.asyncio
async def test_build_timeline_groups_by_doctor_and_slot():
    rows = [
        _row(doctor_id="doc-a", hour=10, minute=0, appt_id=1),
        _row(doctor_id="doc-b", hour=10, minute=30, appt_id=2),
    ]
    session = _FakeSession(rows)
    result = await appointment_service.build_appointments_timeline(
        session,
        1,
        date="2026-06-02",
        slot_minutes=30,
        tz_name="UTC",
    )
    assert result["date"] == "2026-06-02"
    assert "10:00" in result["slots"]
    assert len(result["rows"]) == 2
    doc_a = next(r for r in result["rows"] if r["doctor_id"] == "doc-a")
    idx_1000 = result["slots"].index("10:00")
    assert doc_a["cells"][idx_1000]["appointment"]["id"] == 1


@pytest.mark.asyncio
async def test_build_timeline_unassigned_row():
    rows = [_row(doctor_id=None, hour=9, minute=0, appt_id=3)]
    session = _FakeSession(rows)
    result = await appointment_service.build_appointments_timeline(
        session, 1, date="2026-06-02", tz_name="UTC"
    )
    assert result["rows"][0]["doctor_id"] is None
    assert result["rows"][0]["doctor_label"] == "Без специалиста"


def test_generate_timeline_slots():
    slots = appointment_service.generate_timeline_slots(
        start_hour=9, end_hour=10, slot_minutes=30
    )
    assert slots == ["09:00", "09:30"]


def test_timeline_invalid_date():
    with pytest.raises(ValueError):
        appointment_service._timeline_day_bounds("bad-date", "UTC")

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.db.models import AppointmentStatus
from bot.services import appointment_service


class _ScalarOneResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row
        self.flushed = False

    async def execute(self, _stmt):
        return _ScalarOneResult(self._row)

    async def flush(self):
        self.flushed = True


def _appointment_row(*, status=AppointmentStatus.CONFIRMED):
    org = SimpleNamespace(id=1, name="Org", timezone="UTC")
    customer = SimpleNamespace(
        id=5,
        name="Ali",
        phone="tg:12345",
        org_id=1,
        organization=org,
        dialog_context={"dialog_mode": "manage", "last_appointment_id": 10},
    )
    appt = SimpleNamespace(
        id=10,
        customer_id=5,
        scheduled_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        status=status,
        crm_appointment_id=None,
        crm_doctor_id=None,
        reminder_24h_sent_at=None,
        cancel_reason=None,
    )
    return appt, customer


@pytest.mark.asyncio
async def test_complete_appointment_writes_context_summary():
    appt, customer = _appointment_row()
    session = _FakeSession((appt, customer))
    await appointment_service.complete_appointment(session, 1, 10)
    summary = (customer.dialog_context or {}).get("context_summary", "")
    assert "Запись №10" in summary
    assert "завершена" in summary.lower()
    assert customer.dialog_context.get("dialog_mode") == "booking"

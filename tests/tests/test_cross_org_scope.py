from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bot.db.models import AppointmentStatus
from bot.services import appointment_service, customer_service


class _OneRowResult:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _AppointmentLookupSession:
    def __init__(self, row):
        self.row = row

    async def execute(self, _stmt):
        return _OneRowResult(self.row)


class _CustomerGetSession:
    def __init__(self, customer):
        self.customer = customer

    async def get(self, _model, key):
        if key == self.customer.id:
            return self.customer
        return None


@pytest.mark.asyncio
async def test_get_customer_for_org_wrong_org():
    customer = SimpleNamespace(id=5, org_id=2, name="Other")
    session = _CustomerGetSession(customer)

    result = await customer_service.get_customer_for_org(session, 5, 1)

    assert result is None


@pytest.mark.asyncio
async def test_get_customer_for_org_same_org():
    customer = SimpleNamespace(id=5, org_id=1, name="Mine")
    session = _CustomerGetSession(customer)

    result = await customer_service.get_customer_for_org(session, 5, 1)

    assert result is customer


@pytest.mark.asyncio
async def test_get_appointment_with_customer_wrong_org():
    scheduled = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    org = SimpleNamespace(id=2, name="Other")
    customer = SimpleNamespace(id=5, org_id=2, organization=org)
    appt = SimpleNamespace(id=10, customer_id=5, scheduled_at=scheduled, status=AppointmentStatus.NEW)
    session = _AppointmentLookupSession(None)

    with pytest.raises(appointment_service.AppointmentNotFoundError):
        await appointment_service._get_appointment_with_customer(session, 1, 10)


@pytest.mark.asyncio
async def test_get_appointment_with_customer_same_org():
    scheduled = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    org = SimpleNamespace(id=1, name="Mine")
    customer = SimpleNamespace(id=5, org_id=1, organization=org)
    appt = SimpleNamespace(id=10, customer_id=5, scheduled_at=scheduled, status=AppointmentStatus.NEW)
    session = _AppointmentLookupSession((appt, customer))

    found_appt, found_customer = await appointment_service._get_appointment_with_customer(session, 1, 10)

    assert found_appt is appt
    assert found_customer is customer

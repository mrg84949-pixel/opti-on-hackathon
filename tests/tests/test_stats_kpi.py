from __future__ import annotations

import pytest

from bot.services import stats_service


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeSession:
    def __init__(self, counts: list[int]):
        self._counts = iter(counts)

    async def execute(self, _stmt):
        return _FakeResult(next(self._counts))


@pytest.mark.asyncio
async def test_get_business_stats_includes_30d_kpis():
    session = _FakeSession([10, 4, 2, 1, 3, 5, 1_500_000, 2])
    payload = await stats_service.get_business_stats(session, 1)
    assert payload["org_id"] == 1
    assert payload["total_customers"] == 10
    assert payload["upcoming_appointments_30d"] == 4
    assert payload["new_30d"] == 2
    assert payload["cancelled_30d"] == 1
    assert payload["completed_30d"] == 3
    assert payload["confirmed_30d"] == 5
    assert payload["bookings_30d"] == 10
    assert payload["completed_revenue_30d_minor"] == 1_500_000
    assert payload["completed_revenue_unpriced_count"] == 2
    assert payload["revenue_currency"] == "KZT"


@pytest.mark.asyncio
async def test_get_business_stats_kpis_are_org_scoped_sequence():
    session_a = _FakeSession([5, 1, 0, 0, 0, 0, 0, 0])
    session_b = _FakeSession([8, 2, 3, 2, 1, 4, 250_000, 1])
    payload_a = await stats_service.get_business_stats(session_a, 1)
    payload_b = await stats_service.get_business_stats(session_b, 2)
    assert payload_a["new_30d"] == 0
    assert payload_b["new_30d"] == 3
    assert payload_a["completed_30d"] == 0
    assert payload_b["completed_30d"] == 1
    assert payload_b["confirmed_30d"] == 4
    assert payload_b["bookings_30d"] == 8

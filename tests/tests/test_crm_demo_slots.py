from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.crm.demo_slots import build_demo_slots


def test_build_demo_slots_weekday_returns_three():
    slots = build_demo_slots("2026-05-01")
    assert len(slots) == 3
    assert [slot.start.hour for slot in slots] == [10, 11, 12]


def test_build_demo_slots_sunday_empty():
    assert build_demo_slots("2026-05-03") == []


def test_build_demo_slots_today_filters_past():
    ref = datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc)
    slots = build_demo_slots("2026-05-28", tz_name="UTC", now=ref)
    assert slots == []


def test_build_demo_slots_today_keeps_future():
    ref = datetime(2026, 5, 28, 9, 0, tzinfo=timezone.utc)
    slots = build_demo_slots("2026-05-28", tz_name="UTC", now=ref)
    assert len(slots) == 3
    assert slots[0].start.hour == 10

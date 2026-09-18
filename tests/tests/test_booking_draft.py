from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.llm.booking_draft import (
    PAST_BOOKING_MESSAGE,
    TIME_VAGUE_MESSAGE,
    merge_draft,
    normalize_booking_date,
    normalize_booking_time,
    render_appointment_card,
    validate_draft_for_card,
    validate_draft_schedule,
)


def test_validate_draft_requires_core_fields():
    errors = validate_draft_for_card(merge_draft(None))
    assert "customer_name" in errors[0]
    assert any("service" in e for e in errors)
    assert any("date" in e for e in errors)


def test_render_appointment_card_includes_fields():
    draft = merge_draft(
        None,
        customer_name="Алия",
        service="Консультация",
        date="2026-05-30",
        time="14:30",
        doctor_name="Dr. Smith",
    )
    card = render_appointment_card(draft, tz_name="Asia/Almaty")
    assert "Алия" in card
    assert "Консультация" in card
    assert "30.05.2026 14:30" in card
    assert "Dr. Smith" in card
    assert "Всё верно?" in card


def test_merge_draft_overwrites_fields():
    draft = merge_draft(
        merge_draft(None, customer_name="Old", service="A"),
        customer_name="New",
        date="2026-05-01",
    )
    assert draft["customer_name"] == "New"
    assert draft["service"] == "A"
    assert draft["date"] == "2026-05-01"


def test_validate_draft_schedule_rejects_past_datetime():
    draft = merge_draft(
        None,
        customer_name="Алия",
        service="Консультация",
        date="2024-01-15",
        time="10:00",
    )
    ref = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    errors = validate_draft_schedule(draft, "UTC", now=ref)
    assert any(PAST_BOOKING_MESSAGE in e for e in errors)


def test_validate_draft_schedule_accepts_future_datetime():
    draft = merge_draft(
        None,
        customer_name="Алия",
        service="Консультация",
        date="2026-06-15",
        time="10:00",
    )
    ref = datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc)
    assert validate_draft_schedule(draft, "UTC", now=ref) == []


def test_normalize_booking_date_relative_russian():
    ref = datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc)
    assert normalize_booking_date("завтра", "UTC", now=ref) == "2026-05-29"
    assert normalize_booking_date("на завтра", "UTC", now=ref) == "2026-05-29"
    assert normalize_booking_date("послезавтра", "UTC", now=ref) == "2026-05-30"
    assert normalize_booking_date("сегодня", "UTC", now=ref) == "2026-05-28"
    assert normalize_booking_date("в пятницу", "UTC", now=ref) == "2026-05-29"
    assert normalize_booking_date("29.05.2026", "UTC", now=ref) == "2026-05-29"


def test_normalize_booking_time():
    assert normalize_booking_time("15:00") == "15:00"
    assert normalize_booking_time("в 9:30") == "09:30"
    assert normalize_booking_time("15.00") == "15:00"
    assert normalize_booking_time("вечером") is None


def test_normalize_booking_time_natural_russian():
    assert normalize_booking_time("15") == "15:00"
    assert normalize_booking_time("в 15") == "15:00"
    assert normalize_booking_time("на 15") == "15:00"
    assert normalize_booking_time("15 часов") == "15:00"
    assert normalize_booking_time("9 час") == "09:00"
    assert normalize_booking_time("9.30") == "09:30"
    assert normalize_booking_time("25") is None
    assert normalize_booking_time("вечером") is None
    assert normalize_booking_time("утром") is None

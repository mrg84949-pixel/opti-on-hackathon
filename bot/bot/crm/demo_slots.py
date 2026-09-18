"""Shared demo CRM slot builder for provider adapters."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.crm.base import Slot

DEMO_SLOT_HOURS = (10, 11, 12)
DEMO_SLOT_DURATION_MIN = 30


def _resolve_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def build_demo_slots(
    date_iso: str,
    *,
    tz_name: str = "UTC",
    now: datetime | None = None,
) -> list[Slot]:
    """Demo availability: Mon-Sat with 10/11/12 windows; Sunday closed; no past hours today."""
    day = date.fromisoformat(date_iso)
    if day.weekday() == 6:
        return []

    slots: list[Slot] = []
    for hour in DEMO_SLOT_HOURS:
        start = datetime.fromisoformat(f"{date_iso}T{hour:02d}:00:00")
        end = start + timedelta(minutes=DEMO_SLOT_DURATION_MIN)
        slots.append(Slot(start=start, end=end))

    tz = _resolve_tz(tz_name)
    ref = now or datetime.now(timezone.utc)
    today = ref.astimezone(tz).date()
    if day != today:
        return slots

    now_local = ref.astimezone(tz).replace(tzinfo=None)
    return [slot for slot in slots if slot.start > now_local]

"""Appointment CRUD and status management."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from bot.config import settings
from bot.crm import get_crm_provider
from bot.crm.base import Slot
from bot.db.models import Appointment, AppointmentStatus, Customer, Organization
from bot.llm.booking_draft import PAST_BOOKING_MESSAGE, PENDING_BOOKING_KEY, normalize_booking_date, normalize_booking_time
from bot.llm.context_summary import apply_summary_to_context, build_post_complete_summary

_ACTIVE_STATUSES = (AppointmentStatus.NEW, AppointmentStatus.CONFIRMED)
DEFAULT_CLIENT_CANCEL_REASON = "Отменено клиентом через бота"
UNASSIGNED_DOCTOR_KEY = "__unassigned__"
DEFAULT_TIMELINE_SLOT_MINUTES = 30
TIMELINE_DAY_START_HOUR = 8
TIMELINE_DAY_END_HOUR = 20


class AppointmentNotFoundError(Exception):
    """Appointment missing or not owned by organization."""


class InvalidStatusTransitionError(Exception):
    """Status change is not allowed for current appointment state."""


class InvalidCancelReasonError(Exception):
    """Cancel reason failed validation."""


class ClientChangePendingError(Exception):
    """Appointment already awaits client response to an admin change."""


CLIENT_CHANGE_REJECT_REASON = "Клиент не согласен с изменением"
CLIENT_CHANGE_TIMEOUT_REASON = "Клиент не ответил в течение 24 часов"
SLOT_TAKEN_MESSAGE = "Это время уже занято. Предложи клиенту другое окно."


def _assert_booking_not_in_past(utc_dt: datetime) -> None:
    ref = datetime.now(timezone.utc)
    normalized = utc_dt if utc_dt.tzinfo is not None else utc_dt.replace(tzinfo=timezone.utc)
    if normalized <= ref:
        raise ValueError(PAST_BOOKING_MESSAGE)


class _CRMProviderLike(Protocol):
    async def get_available_slots(
        self, doctor_id: str, date_iso: str, *, tz_name: str | None = None
    ) -> list[Slot]:
        ...

_DIALOG_MODE_KEY = "dialog_mode"
_LAST_APPOINTMENT_ID_KEY = "last_appointment_id"
_DIALOG_MODE_BOOKING = "booking"


def reset_customer_dialog_after_service(
    customer: Customer,
    *,
    context_summary: str | None = None,
) -> None:
    """Return client bot state to booking after service is completed."""
    ctx_data = dict(customer.dialog_context or {})
    ctx_data.pop(PENDING_BOOKING_KEY, None)
    ctx_data[_DIALOG_MODE_KEY] = _DIALOG_MODE_BOOKING
    ctx_data.pop(_LAST_APPOINTMENT_ID_KEY, None)
    if context_summary:
        ctx_data = apply_summary_to_context(ctx_data, context_summary)
    customer.dialog_context = ctx_data


_ALLOWED_TRANSITIONS: dict[AppointmentStatus, set[AppointmentStatus]] = {
    AppointmentStatus.NEW: {AppointmentStatus.CONFIRMED, AppointmentStatus.CANCELLED},
    AppointmentStatus.CONFIRMED: {AppointmentStatus.CANCELLED, AppointmentStatus.COMPLETED},
    AppointmentStatus.CANCELLED: set(),
    AppointmentStatus.COMPLETED: set(),
}


def _status_value(status_obj: object) -> str:
    if hasattr(status_obj, "value"):
        return str(getattr(status_obj, "value"))
    return str(status_obj)


def _normalize_status(status: AppointmentStatus | str) -> AppointmentStatus:
    if isinstance(status, AppointmentStatus):
        return status
    return AppointmentStatus(status)


def _serialize_appointment(appt: Appointment, cust: Customer) -> dict[str, Any]:
    return {
        "id": appt.id,
        "customer_id": cust.id,
        "customer_name": cust.name,
        "customer_phone": cust.phone,
        "scheduled_at": appt.scheduled_at.isoformat(),
        "status": _status_value(appt.status),
        "crm_appointment_id": appt.crm_appointment_id,
        "crm_doctor_id": appt.crm_doctor_id,
        "reminder_24h_sent_at": (
            appt.reminder_24h_sent_at.isoformat() if appt.reminder_24h_sent_at else None
        ),
        "reminder_2h_sent_at": (
            appt.reminder_2h_sent_at.isoformat() if getattr(appt, "reminder_2h_sent_at", None) else None
        ),
        "cancel_reason": getattr(appt, "cancel_reason", None),
        "client_change_requested_at": (
            appt.client_change_requested_at.isoformat()
            if getattr(appt, "client_change_requested_at", None)
            else None
        ),
        "client_change_deadline_at": (
            appt.client_change_deadline_at.isoformat()
            if getattr(appt, "client_change_deadline_at", None)
            else None
        ),
        "client_change_reason": getattr(appt, "client_change_reason", None),
        "service_name": getattr(appt, "service_name", None),
    }


def clear_client_change_state(appt: Appointment) -> None:
    appt.client_change_requested_at = None
    appt.client_change_deadline_at = None
    appt.client_change_reason = None


def has_active_client_change(appt: Appointment) -> bool:
    return getattr(appt, "client_change_deadline_at", None) is not None


def _client_change_expired(appt: Appointment, now: datetime | None = None) -> bool:
    deadline = getattr(appt, "client_change_deadline_at", None)
    if deadline is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return deadline <= ref


def _validate_cancel_reason(reason: str) -> str:
    trimmed = (reason or "").strip()
    if len(trimmed) < 3:
        raise InvalidCancelReasonError("Cancel reason must be at least 3 characters")
    if len(trimmed) > 500:
        raise InvalidCancelReasonError("Cancel reason must be at most 500 characters")
    return trimmed


def _assert_transition(current: AppointmentStatus, target: AppointmentStatus) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidStatusTransitionError(
            f"Cannot transition from {_status_value(current)} to {_status_value(target)}"
        )


async def _get_appointment_with_customer(
    session: AsyncSession, org_id: int, appt_id: int
) -> tuple[Appointment, Customer]:
    stmt = (
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .options(joinedload(Customer.organization))
        .where(Appointment.id == appt_id, Customer.org_id == org_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        raise AppointmentNotFoundError(f"Appointment {appt_id} not found for org {org_id}")
    appt, customer = row
    return appt, customer


def _org_tz(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _timeline_day_bounds(date: str, tz_name: str) -> tuple[datetime, datetime, str]:
    tz = _org_tz(tz_name)
    try:
        day_local = datetime.strptime(date.strip(), "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError as exc:
        raise ValueError("date must be YYYY-MM-DD") from exc
    start_utc = day_local.astimezone(timezone.utc)
    end_utc = (day_local + timedelta(days=1)).astimezone(timezone.utc)
    return start_utc, end_utc, day_local.strftime("%Y-%m-%d")


def generate_timeline_slots(
    *,
    start_hour: int = TIMELINE_DAY_START_HOUR,
    end_hour: int = TIMELINE_DAY_END_HOUR,
    slot_minutes: int = DEFAULT_TIMELINE_SLOT_MINUTES,
) -> list[str]:
    safe_slot = max(15, min(slot_minutes, 120))
    start_m = start_hour * 60
    end_m = end_hour * 60
    slots: list[str] = []
    for minute in range(start_m, end_m, safe_slot):
        h, m = divmod(minute, 60)
        slots.append(f"{h:02d}:{m:02d}")
    return slots


def _local_slot_key(local_dt: datetime, slot_minutes: int) -> str:
    total = local_dt.hour * 60 + local_dt.minute
    snapped = (total // slot_minutes) * slot_minutes
    h, m = divmod(snapped, 60)
    return f"{h:02d}:{m:02d}"


def _doctor_row_key(crm_doctor_id: str | None) -> str:
    trimmed = (crm_doctor_id or "").strip()
    return trimmed or UNASSIGNED_DOCTOR_KEY


def _doctor_row_label(doctor_key: str) -> str:
    if doctor_key == UNASSIGNED_DOCTOR_KEY:
        return "Без специалиста"
    return doctor_key


async def build_appointments_timeline(
    session: AsyncSession,
    org_id: int,
    *,
    date: str | None = None,
    slot_minutes: int = DEFAULT_TIMELINE_SLOT_MINUTES,
    tz_name: str = "UTC",
) -> dict[str, Any]:
    """Grid for admin UI: columns=time slots, rows=crm_doctor_id."""
    tz = _org_tz(tz_name)
    normalized_date = (date or "").strip() or datetime.now(tz).strftime("%Y-%m-%d")
    utc_start, utc_end, normalized_date = _timeline_day_bounds(normalized_date, tz_name)
    safe_slot = max(15, min(slot_minutes, 120))
    slots = generate_timeline_slots(slot_minutes=safe_slot)
    slot_set = set(slots)

    stmt = (
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Customer.org_id == org_id,
            Appointment.scheduled_at >= utc_start,
            Appointment.scheduled_at < utc_end,
        )
        .order_by(Appointment.scheduled_at.asc())
    )
    rows_db = (await session.execute(stmt)).all()

    row_map: dict[str, dict[str, Any]] = {}
    for appt, cust in rows_db:
        local_dt = appt.scheduled_at.astimezone(tz)
        slot_key = _local_slot_key(local_dt, safe_slot)
        if slot_key not in slot_set:
            continue
        doctor_key = _doctor_row_key(appt.crm_doctor_id)
        if doctor_key not in row_map:
            row_map[doctor_key] = {slot: None for slot in slots}
        cell = row_map[doctor_key][slot_key]
        serialized = _serialize_appointment(appt, cust)
        if cell is None:
            row_map[doctor_key][slot_key] = {"appointment": serialized, "extra_count": 0}
        else:
            cell["extra_count"] = int(cell.get("extra_count", 0)) + 1

    timeline_rows = []
    for doctor_key in sorted(row_map.keys(), key=lambda k: (k == UNASSIGNED_DOCTOR_KEY, k)):
        cells_map = row_map[doctor_key]
        timeline_rows.append(
            {
                "doctor_id": None if doctor_key == UNASSIGNED_DOCTOR_KEY else doctor_key,
                "doctor_label": _doctor_row_label(doctor_key),
                "cells": [cells_map[slot] for slot in slots],
            }
        )

    return {
        "date": normalized_date,
        "timezone": tz_name or "UTC",
        "slot_minutes": safe_slot,
        "slots": slots,
        "rows": timeline_rows,
    }


async def list_appointments(
    session: AsyncSession, org_id: int, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    stmt = (
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(Customer.org_id == org_id)
        .order_by(Appointment.scheduled_at.desc())
        .limit(safe_limit)
        .offset(safe_offset)
    )
    rows = (await session.execute(stmt)).all()
    return {
        "items": [_serialize_appointment(appt, cust) for appt, cust in rows],
        "limit": safe_limit,
        "offset": safe_offset,
    }


async def confirm_appointment(
    session: AsyncSession, org_id: int, appt_id: int
) -> tuple[dict[str, Any], Customer]:
    appt, customer = await _get_appointment_with_customer(session, org_id, appt_id)
    current = _normalize_status(appt.status)
    _assert_transition(current, AppointmentStatus.CONFIRMED)
    appt.status = AppointmentStatus.CONFIRMED
    await session.flush()
    return _serialize_appointment(appt, customer), customer


async def auto_confirm_if_enabled(
    session: AsyncSession, org: Organization, appt: Appointment
) -> bool:
    """If org.auto_confirm_appointments, transition NEW → CONFIRMED in-session."""
    if not getattr(org, "auto_confirm_appointments", False):
        return False
    current = _normalize_status(appt.status)
    if current != AppointmentStatus.NEW:
        return False
    _assert_transition(current, AppointmentStatus.CONFIRMED)
    appt.status = AppointmentStatus.CONFIRMED
    await session.flush()
    return True


async def cancel_appointment(
    session: AsyncSession, org_id: int, appt_id: int, reason: str
) -> tuple[dict[str, Any], Customer]:
    validated_reason = _validate_cancel_reason(reason)
    appt, customer = await _get_appointment_with_customer(session, org_id, appt_id)
    current = _normalize_status(appt.status)
    _assert_transition(current, AppointmentStatus.CANCELLED)
    appt.status = AppointmentStatus.CANCELLED
    appt.cancel_reason = validated_reason
    clear_client_change_state(appt)
    await session.flush()
    return _serialize_appointment(appt, customer), customer


async def complete_appointment(
    session: AsyncSession, org_id: int, appt_id: int
) -> tuple[dict[str, Any], Customer]:
    appt, customer = await _get_appointment_with_customer(session, org_id, appt_id)
    current = _normalize_status(appt.status)
    _assert_transition(current, AppointmentStatus.COMPLETED)
    appt.status = AppointmentStatus.COMPLETED
    appt.completed_at = datetime.now(timezone.utc)
    org = customer.organization
    tz_name = (org.timezone if org is not None else None) or "UTC"
    tz = _org_tz(tz_name)
    when_label = appt.scheduled_at.astimezone(tz).strftime("%d.%m.%Y %H:%M")
    summary = build_post_complete_summary(
        appointment_id=appt.id,
        when_label=when_label,
        timezone_name=tz_name,
    )
    reset_customer_dialog_after_service(customer, context_summary=summary)
    await session.flush()
    return _serialize_appointment(appt, customer), customer


def _parse_local_to_utc(date: str, time: str, tz_name: str) -> datetime:
    date_iso = normalize_booking_date(date, tz_name)
    time_hm = normalize_booking_time(time)
    if not date_iso or not time_hm:
        raise ValueError("date/time could not be parsed")
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    local_dt = datetime.strptime(
        f"{date_iso} {time_hm}",
        "%Y-%m-%d %H:%M",
    ).replace(tzinfo=tz)
    return local_dt.astimezone(ZoneInfo("UTC"))


async def _find_slot_conflict(
    session: AsyncSession,
    *,
    org_id: int,
    doctor_id: str | None,
    utc_dt: datetime,
    exclude_appt_id: int | None = None,
) -> Appointment | None:
    if not doctor_id:
        return None
    stmt = (
        select(Appointment)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Customer.org_id == org_id,
            Appointment.crm_doctor_id == doctor_id,
            Appointment.scheduled_at == utc_dt,
            Appointment.status.in_(_ACTIVE_STATUSES),
        )
    )
    if exclude_appt_id is not None:
        stmt = stmt.where(Appointment.id != exclude_appt_id)
    return (await session.execute(stmt)).scalar_one_or_none()


def _draft_time_in_crm_slots(
    local_dt: datetime,
    slots: list[Slot],
    *,
    tz_name: str,
) -> bool:
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    target = local_dt.astimezone(tz).strftime("%H:%M")
    for slot in slots:
        start = slot.start
        if start.tzinfo is None:
            start = start.replace(tzinfo=tz)
        else:
            start = start.astimezone(tz)
        if start.strftime("%H:%M") == target:
            return True
    return False


async def validate_booking_slot_available(
    session: AsyncSession,
    *,
    org: Organization,
    doctor_id: str,
    local_dt: datetime,
    utc_dt: datetime,
    provider: _CRMProviderLike | None = None,
) -> None:
    _assert_booking_not_in_past(utc_dt)
    conflict = await _find_slot_conflict(
        session,
        org_id=org.id,
        doctor_id=doctor_id,
        utc_dt=utc_dt,
    )
    if conflict is not None:
        raise ValueError(SLOT_TAKEN_MESSAGE)
    crm = provider or get_crm_provider(org)
    slots = await crm.get_available_slots(
        doctor_id=doctor_id,
        date_iso=local_dt.strftime("%Y-%m-%d"),
        tz_name=org.timezone or "UTC",
    )
    if not _draft_time_in_crm_slots(local_dt, slots, tz_name=org.timezone or "UTC"):
        raise ValueError(SLOT_TAKEN_MESSAGE)


async def get_active_appointment_for_customer(
    session: AsyncSession, org_id: int, customer_id: int
) -> tuple[Appointment, Customer] | None:
    """Latest active (new/confirmed) appointment for the customer."""
    stmt = (
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Customer.org_id == org_id,
            Customer.id == customer_id,
            Appointment.status.in_(_ACTIVE_STATUSES),
        )
        .order_by(Appointment.scheduled_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return row[0], row[1]


async def cancel_appointment_by_customer(
    session: AsyncSession, org_id: int, customer_id: int, reason: str
) -> tuple[dict[str, Any], Customer]:
    found = await get_active_appointment_for_customer(session, org_id, customer_id)
    if found is None:
        raise AppointmentNotFoundError(
            f"No active appointment for customer {customer_id} in org {org_id}"
        )
    _appt, _customer = found
    trimmed = (reason or "").strip()
    if len(trimmed) < 3:
        trimmed = DEFAULT_CLIENT_CANCEL_REASON
    return await cancel_appointment(session, org_id, _appt.id, trimmed)


async def reschedule_appointment(
    session: AsyncSession,
    org_id: int,
    appt_id: int,
    *,
    date: str,
    time: str,
    doctor_id: str | None = None,
    tz_name: str = "UTC",
) -> tuple[dict[str, Any], Customer, datetime]:
    """Move appointment to a new slot; status returns to NEW for admin re-confirmation."""
    appt, customer = await _get_appointment_with_customer(session, org_id, appt_id)
    current = _normalize_status(appt.status)
    if current not in _ACTIVE_STATUSES:
        raise InvalidStatusTransitionError(
            f"Cannot reschedule appointment in status {_status_value(current)}"
        )

    utc_dt = _parse_local_to_utc(date, time, tz_name)
    _assert_booking_not_in_past(utc_dt)
    new_doctor_id = (doctor_id or "").strip() or appt.crm_doctor_id
    conflict = await _find_slot_conflict(
        session,
        org_id=org_id,
        doctor_id=new_doctor_id,
        utc_dt=utc_dt,
        exclude_appt_id=appt.id,
    )
    if conflict is not None:
        raise ValueError("Это время уже занято. Предложи клиенту другое окно.")

    appt.scheduled_at = utc_dt
    appt.crm_doctor_id = new_doctor_id
    appt.status = AppointmentStatus.NEW
    appt.reminder_24h_sent_at = None
    appt.reminder_2h_sent_at = None
    await session.flush()

    try:
        local_dt = utc_dt.astimezone(ZoneInfo(tz_name or "UTC"))
    except Exception:
        local_dt = utc_dt
    return _serialize_appointment(appt, customer), customer, local_dt


async def propose_appointment_change(
    session: AsyncSession,
    org_id: int,
    appt_id: int,
    *,
    date: str,
    time: str,
    reason: str,
    doctor_id: str | None = None,
    tz_name: str = "UTC",
) -> tuple[dict[str, Any], Customer, datetime]:
    """Admin proposes new slot; client must accept within configured hours."""
    validated_reason = _validate_cancel_reason(reason)
    appt, customer = await _get_appointment_with_customer(session, org_id, appt_id)
    current = _normalize_status(appt.status)
    if current not in _ACTIVE_STATUSES:
        raise InvalidStatusTransitionError(
            f"Cannot change appointment in status {_status_value(current)}"
        )
    if has_active_client_change(appt):
        raise ClientChangePendingError("Client response to previous change is still pending")

    utc_dt = _parse_local_to_utc(date, time, tz_name)
    _assert_booking_not_in_past(utc_dt)
    new_doctor_id = (doctor_id or "").strip() or appt.crm_doctor_id
    conflict = await _find_slot_conflict(
        session,
        org_id=org_id,
        doctor_id=new_doctor_id,
        utc_dt=utc_dt,
        exclude_appt_id=appt.id,
    )
    if conflict is not None:
        raise ValueError("Это время уже занято. Выберите другое окно.")

    now = datetime.now(timezone.utc)
    hours = max(1, int(settings.client_change_response_hours))
    appt.scheduled_at = utc_dt
    appt.crm_doctor_id = new_doctor_id
    appt.client_change_requested_at = now
    appt.client_change_deadline_at = now + timedelta(hours=hours)
    appt.client_change_reason = validated_reason
    appt.reminder_24h_sent_at = None
    appt.reminder_2h_sent_at = None
    await session.flush()

    try:
        local_dt = utc_dt.astimezone(ZoneInfo(tz_name or "UTC"))
    except Exception:
        local_dt = utc_dt
    return _serialize_appointment(appt, customer), customer, local_dt


async def get_appointment_awaiting_client_change(
    session: AsyncSession, org_id: int, customer_id: int
) -> tuple[Appointment, Customer] | None:
    stmt = (
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Customer.org_id == org_id,
            Customer.id == customer_id,
            Appointment.status.in_(_ACTIVE_STATUSES),
            Appointment.client_change_deadline_at.is_not(None),
        )
        .order_by(Appointment.client_change_deadline_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return row[0], row[1]


async def accept_appointment_change(
    session: AsyncSession, org_id: int, customer_id: int
) -> tuple[dict[str, Any], Customer]:
    found = await get_appointment_awaiting_client_change(session, org_id, customer_id)
    if found is None:
        raise AppointmentNotFoundError("No appointment awaiting client confirmation")
    appt, customer = found
    if _client_change_expired(appt):
        raise InvalidStatusTransitionError("Deadline for responding to the change has passed")
    clear_client_change_state(appt)
    appt.status = AppointmentStatus.NEW
    appt.reminder_24h_sent_at = None
    appt.reminder_2h_sent_at = None
    await session.flush()
    return _serialize_appointment(appt, customer), customer


async def reject_appointment_change(
    session: AsyncSession, org_id: int, customer_id: int
) -> tuple[dict[str, Any], Customer]:
    found = await get_appointment_awaiting_client_change(session, org_id, customer_id)
    if found is None:
        raise AppointmentNotFoundError("No appointment awaiting client confirmation")
    appt, customer = found
    if _client_change_expired(appt):
        raise InvalidStatusTransitionError("Deadline for responding to the change has passed")
    clear_client_change_state(appt)
    return await cancel_appointment(session, org_id, appt.id, CLIENT_CHANGE_REJECT_REASON)

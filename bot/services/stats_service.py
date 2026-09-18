"""Dashboard statistics queries extracted from admin_api."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.billing_access import org_bot_enabled
from bot.channels.whatsapp import resolve_whatsapp_provider
from bot.crm.registry import crm_config, resolve_crm_provider_key
from bot.services.org_secrets import secret_is_set
from bot.services.org_services_catalog import org_has_active_services
from bot.services.customer_service import _owner_visible_customer_filter
from bot.services.outbound_health import channel_send_healthy
from bot.config import settings, tenant_env_fallback_allowed
from bot.db.models import Appointment, AppointmentStatus, BotInteractionLog, Customer, Organization


async def get_business_stats(session: AsyncSession, org_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    since_30d = now - timedelta(days=30)
    next_30d = now + timedelta(days=30)

    total_customers = (
        await session.execute(
            select(func.count(Customer.id)).where(
                Customer.org_id == org_id,
                _owner_visible_customer_filter(),
            )
        )
    ).scalar_one()

    upcoming_appointments = (
        await session.execute(
            select(func.count(Appointment.id))
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(
                Customer.org_id == org_id,
                Appointment.scheduled_at >= now,
                Appointment.scheduled_at < next_30d,
                Appointment.status.in_([AppointmentStatus.NEW, AppointmentStatus.CONFIRMED]),
            )
        )
    ).scalar_one()

    async def _count_status(status: AppointmentStatus) -> int:
        result = await session.execute(
            select(func.count(Appointment.id))
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(
                Customer.org_id == org_id,
                Appointment.scheduled_at >= since_30d,
                Appointment.scheduled_at < now,
                Appointment.status == status,
            )
        )
        return int(result.scalar_one())

    new_30d = await _count_status(AppointmentStatus.NEW)
    cancelled_30d = await _count_status(AppointmentStatus.CANCELLED)
    completed_30d = await _count_status(AppointmentStatus.COMPLETED)
    confirmed_30d = await _count_status(AppointmentStatus.CONFIRMED)
    bookings_30d = new_30d + confirmed_30d + completed_30d

    revenue_anchor = func.coalesce(Appointment.completed_at, Appointment.scheduled_at)
    revenue_filters = (
        Customer.org_id == org_id,
        Appointment.status == AppointmentStatus.COMPLETED,
        revenue_anchor >= since_30d,
        revenue_anchor < now,
    )
    revenue_sum = (
        await session.execute(
            select(func.coalesce(func.sum(Appointment.service_price_minor), 0))
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(*revenue_filters)
        )
    ).scalar_one()
    revenue_unpriced = (
        await session.execute(
            select(func.count(Appointment.id))
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(*revenue_filters, Appointment.service_price_minor.is_(None))
        )
    ).scalar_one()

    return {
        "org_id": org_id,
        "total_customers": total_customers,
        "upcoming_appointments_30d": upcoming_appointments,
        "new_30d": new_30d,
        "cancelled_30d": cancelled_30d,
        "completed_30d": completed_30d,
        "confirmed_30d": confirmed_30d,
        "bookings_30d": bookings_30d,
        "completed_revenue_30d_minor": int(revenue_sum),
        "completed_revenue_unpriced_count": int(revenue_unpriced),
        "revenue_currency": "KZT",
    }


def _status_to_key(status_val: object) -> str:
    if hasattr(status_val, "value"):
        return str(getattr(status_val, "value"))
    raw = str(status_val).lower()
    return raw.split(".")[-1] if "." in raw else raw


def _bucket_key(dt: datetime, unit: str) -> str:
    dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if unit == "day":
        return dt.strftime("%Y-%m-%d")
    if unit == "week":
        return dt.strftime("%Y-%m-%d")
    if unit == "month":
        return dt.strftime("%Y-%m")
    return dt.strftime("%Y")


def _day_bucket_starts(now: datetime, n: int = 30) -> list[datetime]:
    today_mid = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return [today_mid - timedelta(days=n - 1 - i) for i in range(n)]


def _week_bucket_starts(now: datetime, n: int = 12) -> list[datetime]:
    today_mid = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    monday = today_mid - timedelta(days=today_mid.weekday())
    return [monday - timedelta(weeks=n - 1 - i) for i in range(n)]


def _month_bucket_starts(now: datetime, n: int = 12) -> list[datetime]:
    y, m = now.year, now.month
    items: list[tuple[int, int]] = []
    cy, cm = y, m
    for _ in range(n):
        items.append((cy, cm))
        cm -= 1
        if cm <= 0:
            cm = 12
            cy -= 1
    items.reverse()
    return [datetime(yy, mm, 1, tzinfo=timezone.utc) for yy, mm in items]


def _year_bucket_starts(now: datetime, n: int = 5) -> list[datetime]:
    y = now.year
    return [datetime(y - (n - 1 - i), 1, 1, tzinfo=timezone.utc) for i in range(n)]


def _empty_activity_row(key: str) -> dict[str, Any]:
    return {"key": key, "new": 0, "confirmed": 0, "completed": 0, "cancelled": 0}


def _merge_activity_rows(
    rows: list[Any],
    bucket_starts: list[datetime],
    unit: Literal["day", "week", "month", "year"],
) -> list[dict[str, Any]]:
    keys = [_bucket_key(b, unit) for b in bucket_starts]
    merged: dict[str, dict[str, Any]] = {k: _empty_activity_row(k) for k in keys}
    for row in rows:
        bd, st, cnt = row[0], row[1], row[2]
        if bd is None:
            continue
        k = _bucket_key(bd, unit)
        if k not in merged:
            continue
        sk = _status_to_key(st)
        if sk in merged[k] and sk != "key":
            merged[k][sk] += int(cnt)
    return [merged[k] for k in keys]


async def _raw_activity_rows(
    session: AsyncSession,
    org_id: int,
    trunc: Literal["day", "week", "month", "year"],
    start: datetime,
    end: datetime,
) -> list[Any]:
    bucket = func.date_trunc(trunc, Appointment.scheduled_at)
    stmt = (
        select(bucket, Appointment.status, func.count(Appointment.id))
        .select_from(Appointment)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Customer.org_id == org_id,
            Appointment.scheduled_at >= start,
            Appointment.scheduled_at < end,
        )
        .group_by(bucket, Appointment.status)
        .order_by(bucket)
    )
    return list((await session.execute(stmt)).all())


async def get_activity_stats(session: AsyncSession, org_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=400)

    day_buckets = _day_bucket_starts(now, 30)
    day_rows = await _raw_activity_rows(session, org_id, "day", day_buckets[0], end)
    series_day = _merge_activity_rows(day_rows, day_buckets, "day")

    week_buckets = _week_bucket_starts(now, 12)
    week_rows = await _raw_activity_rows(session, org_id, "week", week_buckets[0], end)
    series_week = _merge_activity_rows(week_rows, week_buckets, "week")

    month_buckets = _month_bucket_starts(now, 12)
    month_rows = await _raw_activity_rows(session, org_id, "month", month_buckets[0], end)
    series_month = _merge_activity_rows(month_rows, month_buckets, "month")

    year_buckets = _year_bucket_starts(now, 5)
    year_rows = await _raw_activity_rows(session, org_id, "year", year_buckets[0], end)
    series_year = _merge_activity_rows(year_rows, year_buckets, "year")

    return {"day": series_day, "week": series_week, "month": series_month, "year": series_year}


def _whatsapp_connected(org: Organization) -> bool:
    provider = resolve_whatsapp_provider(org)
    if provider == "green":
        return bool((org.whatsapp_instance_id or "").strip() and secret_is_set(org, "whatsapp_api_token"))
    if provider == "meta":
        return bool(
            (org.whatsapp_meta_phone_number_id or "").strip()
            and secret_is_set(org, "whatsapp_meta_access_token")
        )
    return False


def _telegram_connected(org: Organization) -> bool:
    if secret_is_set(org, "telegram_bot_token"):
        return True
    if not tenant_env_fallback_allowed():
        return False
    return bool((settings.telegram_token or "").strip())


def _prompt_tuned(org: Organization) -> bool:
    return len((org.system_prompt or "").strip()) >= 60


def _crm_connected(org: Organization) -> bool:
    key = resolve_crm_provider_key(org)
    if key in ("demo", "none"):
        return True
    if key in ("amocrm", "generic_rest"):
        return bool((org.crm_base_url or "").strip() and secret_is_set(org, "crm_api_token"))
    if key == "yclients":
        company_id = str(crm_config(org).get("company_id") or "").strip()
        return bool(company_id and secret_is_set(org, "crm_api_token"))
    return False


async def get_dashboard_summary(session: AsyncSession, org_id: int) -> dict[str, Any]:
    """Сводка для главного экрана личного кабинета."""
    org = await session.get(Organization, org_id)
    if org is None:
        return {"org_id": org_id, "org_name": "", "found": False}

    base = await get_business_stats(session, org_id)

    now = datetime.now(timezone.utc)
    since_30d = now - timedelta(days=30)
    since_60d = now - timedelta(days=60)

    active_statuses = [AppointmentStatus.NEW, AppointmentStatus.CONFIRMED, AppointmentStatus.COMPLETED]

    bookings_30d = (
        await session.execute(
            select(func.count(Appointment.id))
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(
                Customer.org_id == org_id,
                Appointment.scheduled_at >= since_30d,
                Appointment.scheduled_at < now,
                Appointment.status.in_(active_statuses),
            )
        )
    ).scalar_one()

    bookings_prev_30d = (
        await session.execute(
            select(func.count(Appointment.id))
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(
                Customer.org_id == org_id,
                Appointment.scheduled_at >= since_60d,
                Appointment.scheduled_at < since_30d,
                Appointment.status.in_(active_statuses),
            )
        )
    ).scalar_one()

    completed_30d = (
        await session.execute(
            select(func.count(Appointment.id))
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(
                Customer.org_id == org_id,
                Appointment.scheduled_at >= since_30d,
                Appointment.scheduled_at < now,
                Appointment.status == AppointmentStatus.COMPLETED,
            )
        )
    ).scalar_one()

    unique_contacts_30d = (
        await session.execute(
            select(func.count(func.distinct(BotInteractionLog.external_user_id))).where(
                BotInteractionLog.org_id == org_id,
                BotInteractionLog.created_at >= since_30d,
            )
        )
    ).scalar_one()

    unique_contacts_prev_30d = (
        await session.execute(
            select(func.count(func.distinct(BotInteractionLog.external_user_id))).where(
                BotInteractionLog.org_id == org_id,
                BotInteractionLog.created_at >= since_60d,
                BotInteractionLog.created_at < since_30d,
            )
        )
    ).scalar_one()

    bot_dialogs_30d = (
        await session.execute(
            select(func.count(BotInteractionLog.id)).where(
                BotInteractionLog.org_id == org_id,
                BotInteractionLog.created_at >= since_30d,
            )
        )
    ).scalar_one()

    pending_stmt = (
        select(Appointment, Customer)
        .join(Customer, Customer.id == Appointment.customer_id)
        .where(
            Customer.org_id == org_id,
            Appointment.status == AppointmentStatus.NEW,
        )
        .order_by(Appointment.scheduled_at.asc())
        .limit(8)
    )
    pending_rows = list((await session.execute(pending_stmt)).all())
    pending_items = [
        {
            "id": appt.id,
            "customer_name": cust.name,
            "customer_phone": cust.phone,
            "scheduled_at": appt.scheduled_at.isoformat(),
            "status": _status_to_key(appt.status),
        }
        for appt, cust in pending_rows
    ]

    pending_count = (
        await session.execute(
            select(func.count(Appointment.id))
            .join(Customer, Customer.id == Appointment.customer_id)
            .where(Customer.org_id == org_id, Appointment.status == AppointmentStatus.NEW)
        )
    ).scalar_one()

    return {
        "org_id": org_id,
        "org_name": org.name,
        "found": True,
        "pending_appointments": pending_items,
        "pending_count": int(pending_count),
        "setup": {
            "whatsapp_connected": _whatsapp_connected(org),
            "telegram_connected": _telegram_connected(org),
            "whatsapp_send_healthy": _whatsapp_connected(org)
            and channel_send_healthy(org.id, "whatsapp"),
            "telegram_send_healthy": _telegram_connected(org)
            and channel_send_healthy(org.id, "telegram"),
            "crm_connected": _crm_connected(org),
            "prompt_tuned": _prompt_tuned(org),
            "services_configured": await org_has_active_services(session, org_id),
            "bot_enabled": org_bot_enabled(org),
        },
        "impact": {
            "total_customers": int(base["total_customers"]),
            "upcoming_30d": int(base["upcoming_appointments_30d"]),
            "unique_contacts_30d": int(unique_contacts_30d),
            "unique_contacts_prev_30d": int(unique_contacts_prev_30d),
            "bookings_30d": int(bookings_30d),
            "bookings_prev_30d": int(bookings_prev_30d),
            "completed_30d": int(completed_30d),
            "bot_dialogs_30d": int(bot_dialogs_30d),
        },
    }

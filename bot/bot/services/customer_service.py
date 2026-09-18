"""Customer CRUD, conversation history, and mute."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Appointment, AppointmentStatus, BotInteractionLog, Customer, Organization
from bot.llm.booking_draft import get_pending_booking
from bot.llm.context_summary import get_context_summary
from bot.services import notification_service

ADMIN_SEND_STATUS = "admin_send"
MAX_ADMIN_MESSAGE_CHARS = 2000

NO_SHOW_GRACE_HOURS = 2
AUTO_MUTE_DAYS = 7
AUTO_MUTE_NO_SHOW_THRESHOLD = 3
_ACTIVE_APPOINTMENT_STATUSES = (AppointmentStatus.NEW, AppointmentStatus.CONFIRMED)

MUTE_USER_MESSAGE = (
    "Вы временно ограничены в общении с ботом. "
    "Обратитесь к администратору, если считаете это ошибкой."
)

# Bot-test / sandbox channel phones must not appear in owner «Клиенты» list.
SANDBOX_PHONE_PREFIX = "web:admin-sandbox-"


def _owner_visible_customer_filter():
    return ~Customer.phone.startswith(SANDBOX_PHONE_PREFIX)


def phone_to_external_user_id(phone: str) -> str:
    p = (phone or "").strip()
    for prefix in ("tg:", "wa:", "web:"):
        if p.startswith(prefix):
            return p[len(prefix) :]
    return p


def channel_from_phone(phone: str) -> str:
    p = (phone or "").strip()
    if p.startswith("tg:"):
        return "telegram"
    if p.startswith("wa:"):
        return "whatsapp"
    return "web"


def is_customer_muted(customer: Customer, *, now: datetime | None = None) -> bool:
    until = getattr(customer, "muted_until", None)
    if until is None:
        return False
    ref = now or datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > ref


def reminders_disabled(customer: Customer) -> bool:
    return bool(getattr(customer, "disable_reminders", False))


async def count_no_shows(session: AsyncSession, customer_id: int) -> int:
    """Confirmed visits that started 2h+ ago without completion/cancel."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=NO_SHOW_GRACE_HOURS)
    stmt = (
        select(func.count(Appointment.id))
        .where(
            Appointment.customer_id == customer_id,
            Appointment.status == AppointmentStatus.CONFIRMED,
            Appointment.scheduled_at < cutoff,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def maybe_auto_mute(session: AsyncSession, customer: Customer) -> bool:
    """Apply 7-day mute when no-show count >= 3 and not already muted."""
    if is_customer_muted(customer):
        return False
    no_shows = await count_no_shows(session, customer.id)
    if no_shows < AUTO_MUTE_NO_SHOW_THRESHOLD:
        return False
    customer.muted_until = datetime.now(timezone.utc) + timedelta(days=AUTO_MUTE_DAYS)
    return True


async def get_customer_for_org(
    session: AsyncSession, customer_id: int, org_id: int
) -> Customer | None:
    customer = await session.get(Customer, customer_id)
    if customer is None or customer.org_id != org_id:
        return None
    return customer


async def list_customers(
    session: AsyncSession, org_id: int, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    stmt = (
        select(Customer)
        .where(Customer.org_id == org_id, _owner_visible_customer_filter())
        .order_by(Customer.id.desc())
        .limit(safe_limit)
        .offset(safe_offset)
    )
    rows = (await session.execute(stmt)).scalars().all()
    items = []
    now = datetime.now(timezone.utc)
    for c in rows:
        no_shows = await count_no_shows(session, c.id)
        muted = is_customer_muted(c, now=now)
        items.append(
            {
                "id": c.id,
                "name": c.name,
                "phone": c.phone,
                "muted_until": c.muted_until.isoformat() if c.muted_until else None,
                "is_muted": muted,
                "disable_reminders": reminders_disabled(c),
                "no_show_count": no_shows,
            }
        )
    return {
        "items": items,
        "limit": safe_limit,
        "offset": safe_offset,
    }


def _logs_to_messages(logs: list[BotInteractionLog]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for row in logs:
        at = row.created_at.isoformat()
        base_status = row.status
        user_text = (row.user_message_preview or "").strip()
        if user_text:
            messages.append(
                {
                    "id": f"log-{row.id}-user",
                    "role": "user",
                    "text": user_text,
                    "at": at,
                    "status": base_status,
                }
            )
        reply_text = (row.reply_preview or "").strip()
        if reply_text:
            role = "admin" if row.status == ADMIN_SEND_STATUS else "assistant"
            messages.append(
                {
                    "id": f"log-{row.id}-bot",
                    "role": role,
                    "text": reply_text,
                    "at": at,
                    "status": base_status,
                }
            )
    return messages


async def get_conversation(
    session: AsyncSession,
    customer_id: int,
    org_id: int,
    *,
    limit: int = 100,
) -> dict[str, Any] | None:
    customer = await get_customer_for_org(session, customer_id, org_id)
    if customer is None:
        return None

    await maybe_auto_mute(session, customer)
    await session.flush()

    external_id = phone_to_external_user_id(customer.phone)
    channel = channel_from_phone(customer.phone)
    safe_limit = max(1, min(limit, 500))

    stmt = (
        select(BotInteractionLog)
        .where(
            BotInteractionLog.org_id == org_id,
            BotInteractionLog.external_user_id == external_id,
        )
        .order_by(BotInteractionLog.created_at.asc())
        .limit(safe_limit)
    )
    logs = list((await session.execute(stmt)).scalars().all())
    no_shows = await count_no_shows(session, customer.id)

    return {
        "customer_id": customer.id,
        "org_id": org_id,
        "channel": channel,
        "external_user_id": external_id,
        "muted_until": customer.muted_until.isoformat() if customer.muted_until else None,
        "no_show_count": no_shows,
        "messages": _logs_to_messages(logs),
    }


async def mute_for_human_handoff(
    session: AsyncSession, customer_id: int, org_id: int
) -> bool:
    """Mute customer for AUTO_MUTE_DAYS after handoff; skip if already muted."""
    customer = await get_customer_for_org(session, customer_id, org_id)
    if customer is None or is_customer_muted(customer):
        return False
    await set_mute(session, customer_id, org_id)
    return True


async def set_mute(
    session: AsyncSession,
    customer_id: int,
    org_id: int,
    *,
    days: int | None = None,
    muted_until: datetime | None = None,
) -> dict[str, Any] | None:
    customer = await get_customer_for_org(session, customer_id, org_id)
    if customer is None:
        return None
    now = datetime.now(timezone.utc)
    if muted_until is not None:
        until = muted_until
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        customer.muted_until = until
    elif days is not None:
        safe_days = max(1, min(days, 365))
        customer.muted_until = now + timedelta(days=safe_days)
    else:
        customer.muted_until = now + timedelta(days=AUTO_MUTE_DAYS)
    no_shows = await count_no_shows(session, customer.id)
    return {
        "customer_id": customer.id,
        "muted_until": customer.muted_until.isoformat() if customer.muted_until else None,
        "no_show_count": no_shows,
    }


async def clear_mute(
    session: AsyncSession, customer_id: int, org_id: int
) -> dict[str, Any] | None:
    customer = await get_customer_for_org(session, customer_id, org_id)
    if customer is None:
        return None
    customer.muted_until = None
    no_shows = await count_no_shows(session, customer.id)
    return {
        "customer_id": customer.id,
        "muted_until": None,
        "no_show_count": no_shows,
    }


async def set_reminders_disabled(
    session: AsyncSession,
    customer_id: int,
    org_id: int,
    *,
    disabled: bool,
) -> dict[str, Any] | None:
    customer = await get_customer_for_org(session, customer_id, org_id)
    if customer is None:
        return None
    customer.disable_reminders = disabled
    return {
        "customer_id": customer.id,
        "disable_reminders": customer.disable_reminders,
    }


def _truncate(text: str | None, *, max_len: int = 160) -> str | None:
    if not text:
        return None
    trimmed = text.strip()
    if not trimmed:
        return None
    if len(trimmed) <= max_len:
        return trimmed
    return trimmed[: max_len - 1] + "…"


def _serialize_appointment_row(appt: Appointment) -> dict[str, Any]:
    status = appt.status.value if hasattr(appt.status, "value") else str(appt.status)
    return {
        "id": appt.id,
        "scheduled_at": appt.scheduled_at.isoformat(),
        "status": status,
        "cancel_reason": getattr(appt, "cancel_reason", None),
        "crm_appointment_id": appt.crm_appointment_id,
    }


async def _appointment_stats(session: AsyncSession, customer_id: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    total_stmt = select(func.count(Appointment.id)).where(Appointment.customer_id == customer_id)
    total = int((await session.execute(total_stmt)).scalar_one())
    upcoming_stmt = select(func.count(Appointment.id)).where(
        Appointment.customer_id == customer_id,
        Appointment.scheduled_at >= now,
        Appointment.status.in_(_ACTIVE_APPOINTMENT_STATUSES),
    )
    upcoming = int((await session.execute(upcoming_stmt)).scalar_one())
    last_stmt = (
        select(Appointment)
        .where(Appointment.customer_id == customer_id)
        .order_by(Appointment.scheduled_at.desc())
        .limit(1)
    )
    last_appt = (await session.execute(last_stmt)).scalars().first()
    last_at = last_appt.scheduled_at.isoformat() if last_appt else None
    last_status = None
    if last_appt is not None:
        last_status = (
            last_appt.status.value if hasattr(last_appt.status, "value") else str(last_appt.status)
        )
    return {
        "appointments_total": total,
        "appointments_upcoming": upcoming,
        "last_appointment_at": last_at,
        "last_appointment_status": last_status,
    }


async def _last_user_message_preview(
    session: AsyncSession, org_id: int, phone: str
) -> tuple[str | None, str | None]:
    external_id = phone_to_external_user_id(phone)
    stmt = (
        select(BotInteractionLog)
        .where(
            BotInteractionLog.org_id == org_id,
            BotInteractionLog.external_user_id == external_id,
        )
        .order_by(BotInteractionLog.created_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        return None, None
    text = _truncate(row.user_message_preview)
    at = row.created_at.isoformat()
    return text, at


async def _customer_database_fields(session: AsyncSession, customer: Customer) -> dict[str, Any]:
    stats = await _appointment_stats(session, customer.id)
    last_msg, last_msg_at = await _last_user_message_preview(session, customer.org_id, customer.phone)
    summary = get_context_summary(customer.dialog_context)
    draft = get_pending_booking(customer.dialog_context)
    pending_hint = None
    if draft:
        parts = [
            p
            for p in (
                (draft.get("service") or "").strip() or None,
                (draft.get("date") or "").strip() or None,
                (draft.get("time") or "").strip() or None,
            )
            if p
        ]
        if parts:
            pending_hint = " · ".join(parts)
    return {
        **stats,
        "context_summary": _truncate(summary, max_len=240),
        "last_user_message": last_msg,
        "last_message_at": last_msg_at,
        "pending_booking_hint": pending_hint,
    }


async def list_customers_database(
    session: AsyncSession, org_id: int, *, limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    base = await list_customers(session, org_id, limit=limit, offset=offset)
    enriched: list[dict[str, Any]] = []
    for item in base["items"]:
        customer = await get_customer_for_org(session, item["id"], org_id)
        if customer is None:
            continue
        extra = await _customer_database_fields(session, customer)
        enriched.append({**item, **extra})
    return {**base, "items": enriched}


async def get_customer_profile(
    session: AsyncSession, customer_id: int, org_id: int, *, appointments_limit: int = 20
) -> dict[str, Any] | None:
    customer = await get_customer_for_org(session, customer_id, org_id)
    if customer is None:
        return None

    now = datetime.now(timezone.utc)
    no_shows = await count_no_shows(session, customer.id)
    safe_appt_limit = max(1, min(appointments_limit, 50))

    appt_stmt = (
        select(Appointment)
        .where(Appointment.customer_id == customer.id)
        .order_by(Appointment.scheduled_at.desc())
        .limit(safe_appt_limit)
    )
    appointments = [
        _serialize_appointment_row(a) for a in (await session.execute(appt_stmt)).scalars().all()
    ]

    external_id = phone_to_external_user_id(customer.phone)
    log_stmt = (
        select(BotInteractionLog)
        .where(
            BotInteractionLog.org_id == org_id,
            BotInteractionLog.external_user_id == external_id,
        )
        .order_by(BotInteractionLog.created_at.desc())
        .limit(8)
    )
    logs = list((await session.execute(log_stmt)).scalars().all())
    recent_messages: list[dict[str, Any]] = []
    for row in reversed(logs):
        text = (row.user_message_preview or "").strip()
        if not text:
            continue
        recent_messages.append(
            {
                "text": text,
                "at": row.created_at.isoformat(),
                "status": row.status,
            }
        )

    summary = get_context_summary(customer.dialog_context)
    draft = get_pending_booking(customer.dialog_context)
    stats = await _appointment_stats(session, customer.id)

    return {
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "phone": customer.phone,
            "channel": channel_from_phone(customer.phone),
            "muted_until": customer.muted_until.isoformat() if customer.muted_until else None,
            "is_muted": is_customer_muted(customer, now=now),
            "disable_reminders": reminders_disabled(customer),
            "no_show_count": no_shows,
        },
        "context_summary": summary,
        "pending_booking": draft,
        "recent_messages": recent_messages,
        "appointments": appointments,
        **stats,
    }


def _preview_text(text: str, max_len: int = 512) -> str:
    trimmed = text.strip()
    if len(trimmed) <= max_len:
        return trimmed
    return trimmed[: max_len - 1] + "…"


async def send_admin_message(
    session: AsyncSession,
    customer_id: int,
    org_id: int,
    *,
    text: str,
) -> dict[str, Any] | None:
    """Send manual admin message to customer via TG/WA and append BotInteractionLog."""
    customer = await get_customer_for_org(session, customer_id, org_id)
    if customer is None:
        return None

    trimmed = text.strip()
    if not trimmed or len(trimmed) > MAX_ADMIN_MESSAGE_CHARS:
        return None

    phone = (customer.phone or "").strip()
    if not phone.startswith(("tg:", "wa:")):
        return {
            "error_code": "unsupported_channel",
            "customer_id": customer_id,
            "channel": channel_from_phone(customer.phone),
        }

    org = await session.get(Organization, org_id)
    if org is None:
        return None

    channel = channel_from_phone(customer.phone)
    result = await notification_service.send_customer_message(org, customer, trimmed)

    session.add(
        BotInteractionLog(
            org_id=org_id,
            channel=channel[:32],
            external_user_id=phone_to_external_user_id(customer.phone)[:255],
            user_message_preview="",
            reply_preview=_preview_text(trimmed),
            status=ADMIN_SEND_STATUS,
        )
    )
    await session.flush()

    return {
        "customer_id": customer_id,
        "message_preview": _preview_text(trimmed),
        "notification_sent": bool(result.ok),
        "auth_failed": bool(result.auth_failed),
        "channel": result.channel or channel,
    }


def normalize_customer_phone(
    *,
    phone: str | None = None,
    telegram_id: str | None = None,
    whatsapp_id: str | None = None,
) -> str:
    """Normalize channel phone for customer lookup (dev/CLI helpers)."""
    if phone:
        p = phone.strip()
        if p.startswith(("tg:", "wa:", "web:")):
            return p
        return p
    if telegram_id:
        return f"tg:{str(telegram_id).strip()}"
    if whatsapp_id:
        return f"wa:{str(whatsapp_id).strip().lstrip('+')}"
    raise ValueError("phone, telegram_id, or whatsapp_id is required")


async def get_customer_by_phone(
    session: AsyncSession, org_id: int, phone: str
) -> Customer | None:
    stmt = select(Customer).where(Customer.org_id == org_id, Customer.phone == phone)
    return (await session.execute(stmt)).scalar_one_or_none()


async def reset_customer_for_org(
    session: AsyncSession,
    customer_id: int,
    org_id: int,
    *,
    clear_name: bool = True,
    clear_llm_session: bool = True,
) -> dict[str, Any] | None:
    """Reset customer to a clean booking state for dev/testing (keeps customer row)."""
    customer = await get_customer_for_org(session, customer_id, org_id)
    if customer is None:
        return None

    external_id = phone_to_external_user_id(customer.phone)
    channel = channel_from_phone(customer.phone)

    log_result = await session.execute(
        delete(BotInteractionLog).where(
            BotInteractionLog.org_id == org_id,
            BotInteractionLog.external_user_id == external_id,
        )
    )
    logs_removed = int(log_result.rowcount or 0)

    appt_result = await session.execute(
        delete(Appointment).where(Appointment.customer_id == customer_id)
    )
    appointments_removed = int(appt_result.rowcount or 0)

    customer.dialog_context = {}
    customer.muted_until = None
    customer.disable_reminders = False
    if clear_name:
        customer.name = None

    await session.flush()

    if clear_llm_session:
        from bot.llm import llm_engine

        llm_engine.clear_in_memory_session(
            channel=channel,
            user_id=external_id,
            org_id=org_id,
        )

    return {
        "customer_id": customer_id,
        "org_id": org_id,
        "phone": customer.phone,
        "logs_removed": logs_removed,
        "appointments_removed": appointments_removed,
        "name_cleared": clear_name,
        "llm_session_cleared": clear_llm_session,
    }


async def erase_customer_for_org(
    session: AsyncSession, customer_id: int, org_id: int
) -> dict[str, Any] | None:
    """Hard-delete customer PII: interaction logs, appointments (CASCADE), customer row."""
    customer = await get_customer_for_org(session, customer_id, org_id)
    if customer is None:
        return None

    external_id = phone_to_external_user_id(customer.phone)
    log_result = await session.execute(
        delete(BotInteractionLog).where(
            BotInteractionLog.org_id == org_id,
            BotInteractionLog.external_user_id == external_id,
        )
    )
    logs_removed = int(log_result.rowcount or 0)

    appointments_removed = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Appointment)
                .where(Appointment.customer_id == customer_id)
            )
        ).scalar_one()
        or 0
    )

    await session.delete(customer)
    await session.flush()

    return {
        "customer_id": customer_id,
        "org_id": org_id,
        "logs_removed": logs_removed,
        "appointments_removed": appointments_removed,
    }

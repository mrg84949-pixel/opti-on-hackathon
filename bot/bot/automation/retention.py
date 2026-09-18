from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, select
from sqlalchemy.orm import joinedload

from bot.billing_access import org_bot_operational
from bot.config import settings
from bot.db.database import AsyncSessionLocal
from bot.db.models import Appointment, AppointmentStatus, Customer, Organization
from bot.logging_config import get_logger
from bot.services import customer_service, notification_service
from bot.services.outbound_notify import handle_reminder_style_outbound
from bot.services.outbound_result import OutboundSendResult

logger = get_logger(__name__)


def _retention_days(org: Organization) -> int:
    raw = getattr(org, "retention_days_after_complete", None)
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _in_retention_window(
    completed_at: datetime,
    *,
    now: datetime,
    days: int,
    window_minutes: int,
) -> bool:
    target_from = now - timedelta(days=days)
    target_to = target_from + timedelta(minutes=window_minutes)
    if completed_at.tzinfo is None:
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    return target_from <= completed_at < target_to


async def process_retention_followups() -> None:
    """
    For orgs with retention_days_after_complete >= 1:
    send follow-up to clients N days after appointment complete (by completed_at).
    """
    window_minutes = settings.reminder_window_minutes
    now = datetime.now(timezone.utc)

    stmt: Select[tuple[Appointment]] = (
        select(Appointment)
        .options(joinedload(Appointment.customer).joinedload(Customer.organization))
        .where(
            Appointment.status == AppointmentStatus.COMPLETED,
            Appointment.completed_at.is_not(None),
            Appointment.retention_sent_at.is_(None),
            Appointment.completed_at <= now,
        )
    )

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()
        for appt in rows:
            customer = appt.customer
            org = customer.organization if customer else None
            if not customer or not org:
                continue
            days = _retention_days(org)
            if days < 1:
                continue
            completed_at = appt.completed_at
            if completed_at is None or not _in_retention_window(
                completed_at, now=now, days=days, window_minutes=window_minutes
            ):
                continue
            if not org_bot_operational(org):
                logger.debug(
                    "Retention skipped for non-operational org",
                    extra={
                        "extra_data": {
                            "event": "retention_skipped_frozen",
                            "org_id": org.id,
                            "appointment_id": appt.id,
                        }
                    },
                )
                continue
            if customer_service.is_customer_muted(customer, now=now):
                logger.debug(
                    "Retention skipped for muted customer",
                    extra={
                        "extra_data": {
                            "event": "retention_skipped_muted",
                            "customer_id": customer.id,
                            "appointment_id": appt.id,
                        }
                    },
                )
                continue
            if customer_service.reminders_disabled(customer):
                logger.debug(
                    "Retention skipped for reminder opt-out",
                    extra={
                        "extra_data": {
                            "event": "retention_skipped_disabled",
                            "customer_id": customer.id,
                            "appointment_id": appt.id,
                        }
                    },
                )
                continue
            text = notification_service.render_retention_text(org, customer)
            try:
                result = await notification_service.send_customer_message(org, customer, text)
            except Exception as exc:
                logger.exception(
                    "Retention send failed",
                    extra={
                        "extra_data": {
                            "event": "retention_send_error",
                            "customer_id": customer.id,
                            "org_id": org.id,
                            "appointment_id": appt.id,
                            "error_type": type(exc).__name__,
                        }
                    },
                )
                result = OutboundSendResult.retryable_error()
            handle_reminder_style_outbound(
                appt,
                "retention_sent_at",
                result,
                org_id=org.id,
                log_event="retention_auth_failed_suppressed",
                extra={"customer_id": customer.id, "appointment_id": appt.id},
            )
        await session.commit()

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, select
from sqlalchemy.orm import joinedload

from bot.automation.fair_queue import fair_reminder_order
from bot.billing_access import org_bot_operational
from bot.config import settings
from bot.debug_log import debug_log
from bot.db.database import AsyncSessionLocal
from bot.db.models import Appointment, AppointmentStatus, Customer
from bot.logging_config import get_logger
from bot.services import customer_service, notification_service
from bot.services.outbound_notify import handle_reminder_style_outbound
from bot.services.outbound_result import OutboundSendResult

logger = get_logger(__name__)


def _org_id_from_appt(appt: Appointment) -> int | None:
    customer = appt.customer
    if customer is None:
        return None
    org = customer.organization
    if org is None or not hasattr(org, "id"):
        return None
    return org.id


async def _process_reminders(
    *,
    hours_before: int,
    sent_attr: str,
    render_text: Callable[[Appointment], str],
    log_prefix: str,
    send_error_event: str,
) -> None:
    window_minutes = settings.reminder_window_minutes
    now = datetime.now(timezone.utc)
    target_from = now + timedelta(hours=hours_before)
    target_to = target_from + timedelta(minutes=window_minutes)
    debug_log(
        run_id="audit-pre",
        hypothesis_id="H5",
        location=f"bot/automation/reminders.py:{log_prefix}:window",
        message="Reminder window calculated",
        data={
            "window_minutes": window_minutes,
            "hours_before": hours_before,
            "target_from_iso": target_from.isoformat(),
        },
    )

    sent_column = getattr(Appointment, sent_attr)
    stmt: Select[tuple[Appointment]] = (
        select(Appointment)
        .options(joinedload(Appointment.customer).joinedload(Customer.organization))
        .where(
            Appointment.status.in_([AppointmentStatus.NEW, AppointmentStatus.CONFIRMED]),
            sent_column.is_(None),
            Appointment.scheduled_at >= target_from,
            Appointment.scheduled_at < target_to,
        )
    )

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H5",
            location=f"bot/automation/reminders.py:{log_prefix}:rows",
            message="Reminder candidates selected",
            data={"candidates_count": len(rows), "hours_before": hours_before},
        )
        ordered = fair_reminder_order(
            list(rows),
            org_id_fn=_org_id_from_appt,
            max_per_org=settings.reminder_fair_max_per_org_per_tick,
        )
        orgs_touched: set[int] = set()
        for appt in ordered:
            customer = appt.customer
            org = customer.organization if customer else None
            if not customer or not org:
                continue
            orgs_touched.add(org.id)
            if not org_bot_operational(org):
                logger.debug(
                    "Reminder skipped for non-operational org",
                    extra={
                        "extra_data": {
                            "event": "reminder_skipped_frozen",
                            "org_id": org.id if hasattr(org, "id") else None,
                            "hours_before": hours_before,
                        }
                    },
                )
                continue
            if customer_service.reminders_disabled(customer):
                logger.debug(
                    "Reminder skipped for customer opt-out",
                    extra={
                        "extra_data": {
                            "event": "reminder_skipped_disabled",
                            "customer_id": customer.id if hasattr(customer, "id") else None,
                            "org_id": org.id if hasattr(org, "id") else None,
                            "hours_before": hours_before,
                        }
                    },
                )
                continue
            if customer_service.is_customer_muted(customer, now=now):
                logger.debug(
                    "Reminder skipped for muted customer",
                    extra={
                        "extra_data": {
                            "event": "reminder_skipped_muted",
                            "customer_id": customer.id if hasattr(customer, "id") else None,
                            "org_id": org.id if hasattr(org, "id") else None,
                            "hours_before": hours_before,
                        }
                    },
                )
                continue
            text = render_text(appt)
            try:
                result = await notification_service.send_customer_message(org, customer, text)
            except Exception as exc:
                logger.exception(
                    "Reminder send failed",
                    extra={
                        "extra_data": {
                            "event": send_error_event,
                            "customer_id": customer.id if hasattr(customer, "id") else None,
                            "org_id": org.id if hasattr(org, "id") else None,
                            "error_type": type(exc).__name__,
                            "hours_before": hours_before,
                        }
                    },
                )
                result = OutboundSendResult.retryable_error()
            handle_reminder_style_outbound(
                appt,
                sent_attr,
                result,
                org_id=org.id,
                log_event="outbound_auth_failed_suppressed",
                extra={
                    "customer_id": customer.id if hasattr(customer, "id") else None,
                    "hours_before": hours_before,
                },
            )
        if rows:
            logger.info(
                "Reminder fair queue tick",
                extra={
                    "extra_data": {
                        "event": "reminder_fair_tick",
                        "hours_before": hours_before,
                        "candidates": len(rows),
                        "processed": len(ordered),
                        "deferred_by_cap": max(0, len(rows) - len(ordered)),
                        "orgs_touched": len(orgs_touched),
                    }
                },
            )
        await session.commit()


async def process_24h_reminders() -> None:
    """
    По расписанию (см. scheduler):
    - находит NEW/CONFIRMED записи в окне [now+24h, now+24h+window),
    - отправляет напоминание,
    - помечает reminder_24h_sent_at, чтобы не дублировать отправку.
    """
    await _process_reminders(
        hours_before=24,
        sent_attr="reminder_24h_sent_at",
        render_text=lambda appt: notification_service.render_24h_reminder_text(
            appt.scheduled_at,
            org_name=getattr(getattr(appt.customer, "organization", None), "name", None),
        ),
        log_prefix="process_24h_reminders",
        send_error_event="reminder_send_error",
    )


async def process_2h_reminders() -> None:
    """
    По расписанию (см. scheduler):
    - находит NEW/CONFIRMED записи в окне [now+2h, now+2h+window),
    - отправляет напоминание,
    - помечает reminder_2h_sent_at, чтобы не дублировать отправку.
    """
    await _process_reminders(
        hours_before=2,
        sent_attr="reminder_2h_sent_at",
        render_text=lambda appt: notification_service.render_2h_reminder_text(
            appt.scheduled_at,
            org_name=getattr(getattr(appt.customer, "organization", None), "name", None),
        ),
        log_prefix="process_2h_reminders",
        send_error_event="reminder_2h_send_error",
    )

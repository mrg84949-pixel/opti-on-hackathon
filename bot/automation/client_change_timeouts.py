from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from bot.billing_access import org_bot_operational
from bot.config import settings
from bot.db.database import AsyncSessionLocal
from bot.db.models import Appointment, AppointmentStatus, Customer
from bot.logging_config import get_logger
from bot.services import appointment_service, notification_service
from bot.services.outbound_result import OutboundSendResult

logger = get_logger(__name__)


async def process_client_change_timeouts() -> None:
    """Cancel appointments where client did not respond to admin change before deadline."""
    now = datetime.now(timezone.utc)
    stmt = (
        select(Appointment)
        .options(joinedload(Appointment.customer).joinedload(Customer.organization))
        .where(
            Appointment.client_change_deadline_at.is_not(None),
            Appointment.client_change_deadline_at <= now,
            Appointment.status.in_(
                (AppointmentStatus.NEW, AppointmentStatus.CONFIRMED)
            ),
        )
    )

    async with AsyncSessionLocal() as session:
        rows = (await session.execute(stmt)).scalars().all()
        for appt in rows:
            customer = appt.customer
            org = customer.organization if customer else None
            if not customer or not org:
                continue
            if not org_bot_operational(org):
                continue
            scheduled_at = appt.scheduled_at
            tz_name = org.timezone or "UTC"
            text = notification_service.render_client_change_timeout_text(
                scheduled_at, tz_name
            )
            try:
                result = await notification_service.send_customer_message(org, customer, text)
            except Exception as exc:
                logger.exception(
                    "Client change timeout notify failed",
                    extra={
                        "extra_data": {
                            "event": "client_change_timeout_notify_error",
                            "appointment_id": appt.id,
                            "org_id": org.id,
                            "error_type": type(exc).__name__,
                        }
                    },
                )
                result = OutboundSendResult.retryable_error()
            if not result.ok and not result.auth_failed:
                continue
            if result.auth_failed:
                logger.warning(
                    "Client change timeout notify auth failure; proceeding with cancel",
                    extra={
                        "extra_data": {
                            "event": "client_change_timeout_auth_failed",
                            "appointment_id": appt.id,
                            "org_id": org.id,
                            "channel": result.channel,
                        }
                    },
                )
            try:
                await appointment_service.cancel_appointment(
                    session,
                    org.id,
                    appt.id,
                    appointment_service.CLIENT_CHANGE_TIMEOUT_REASON,
                )
            except appointment_service.AppointmentNotFoundError:
                continue
            except appointment_service.InvalidStatusTransitionError:
                appointment_service.clear_client_change_state(appt)
                await session.flush()
                continue
        await session.commit()

"""CRM → bot appointment sync (cancel MVP)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from bot.billing_access import org_bot_operational
from bot.crm.factory import get_crm_provider
from bot.crm.registry import crm_config, resolve_crm_provider_key
from bot.db.models import Appointment, AppointmentStatus, Customer, Organization
from bot.logging_config import get_logger
from bot.services import appointment_service, notification_service
from bot.services.org_secrets import secret_is_set

logger = get_logger(__name__)

CRM_CANCEL_REASON = "Отменено в CRM"
CRM_SYNC_WATERMARK_KEY = "last_appointment_sync_at"
CRM_SYNC_LOOKBACK_HOURS = 24
EXTERNAL_CRM_KEYS = frozenset({"amocrm", "generic_rest", "yclients", "macdent"})


def _crm_sync_eligible(org: Organization) -> bool:
    key = resolve_crm_provider_key(org)
    if key not in EXTERNAL_CRM_KEYS:
        return False
    provider = get_crm_provider(org)
    if bool(getattr(provider, "demo_mode", False)):
        return False
    if key == "yclients":
        company_id = str(crm_config(org).get("company_id") or "").strip()
        return (
            bool(company_id)
            and secret_is_set(org, "crm_api_token")
            and secret_is_set(org, "crm_user_token")
        )
    if key == "macdent":
        # Fixed base URL (not client-configurable) — only the token is required.
        return secret_is_set(org, "crm_api_token")
    return bool((org.crm_base_url or "").strip() and secret_is_set(org, "crm_api_token"))


def _parse_watermark(org: Organization) -> datetime | None:
    raw = crm_config(org).get(CRM_SYNC_WATERMARK_KEY)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _set_sync_watermark(org: Organization, when: datetime) -> None:
    cfg = dict(crm_config(org))
    cfg[CRM_SYNC_WATERMARK_KEY] = when.astimezone(timezone.utc).isoformat()
    org.crm_config = cfg or None


async def get_appointment_by_crm_id(
    session: AsyncSession,
    org_id: int,
    crm_appointment_id: str,
) -> Appointment | None:
    stmt = (
        select(Appointment)
        .join(Customer, Appointment.customer_id == Customer.id)
        .options(joinedload(Appointment.customer).joinedload(Customer.organization))
        .where(
            Customer.org_id == org_id,
            Appointment.crm_appointment_id == crm_appointment_id,
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def apply_crm_cancellation(
    session: AsyncSession,
    org: Organization,
    org_id: int,
    appt: Appointment,
) -> str:
    """Shared tail for both sync strategies: cancel + notify + reset dialog.

    Returns "synced", "skipped", or "error" for the caller's counters.
    """
    if appt.status == AppointmentStatus.CANCELLED:
        return "skipped"

    customer = appt.customer
    if customer is None:
        return "skipped"

    try:
        item, returned_customer = await appointment_service.cancel_appointment(
            session, org_id, appt.id, CRM_CANCEL_REASON
        )
    except appointment_service.InvalidStatusTransitionError:
        return "skipped"
    except Exception as exc:
        logger.warning(
            "CRM sync cancel failed",
            extra={
                "extra_data": {
                    "event": "crm_sync_cancel_failed",
                    "org_id": org_id,
                    "appointment_id": appt.id,
                    "error": str(exc)[:200],
                }
            },
        )
        return "error"

    scheduled_at = datetime.fromisoformat(item["scheduled_at"])
    message = notification_service.render_cancel_text(scheduled_at, CRM_CANCEL_REASON)
    try:
        await notification_service.send_customer_message(org, returned_customer, message)
    except Exception as exc:
        logger.warning(
            "CRM sync cancel notify failed",
            extra={
                "extra_data": {
                    "event": "crm_sync_cancel_notify_failed",
                    "org_id": org_id,
                    "appointment_id": appt.id,
                    "error_type": type(exc).__name__,
                }
            },
        )

    appointment_service.reset_customer_dialog_after_service(returned_customer)
    logger.info(
        "CRM sync cancelled appointment",
        extra={"extra_data": {"event": "crm_sync_cancel", "org_id": org_id, "appointment_id": appt.id}},
    )
    return "synced"


async def apply_crm_reschedule(
    session: AsyncSession,
    org: Organization,
    org_id: int,
    appt: Appointment,
    new_scheduled_at_utc: datetime,
) -> str:
    """CRM-driven reschedule: staff moved the booking's time directly in the
    CRM (MacDent zapis CHANGE event) — mirror it locally. Unlike a
    client-initiated reschedule (appointment_service.reschedule_appointment),
    this does NOT reset status back to NEW for re-confirmation: the clinic
    already acted in their own system, so there's nothing for our admin to
    re-approve — we're just catching our local copy up to match reality.

    Returns "synced", "skipped", or "error".
    """
    if appt.status not in (AppointmentStatus.NEW, AppointmentStatus.CONFIRMED):
        return "skipped"
    if appt.scheduled_at == new_scheduled_at_utc:
        return "skipped"

    customer = appt.customer
    if customer is None:
        return "skipped"

    appt.scheduled_at = new_scheduled_at_utc
    appt.reminder_24h_sent_at = None
    appt.reminder_2h_sent_at = None
    try:
        await session.flush()
    except Exception as exc:
        logger.warning(
            "CRM sync reschedule failed",
            extra={
                "extra_data": {
                    "event": "crm_sync_reschedule_failed",
                    "org_id": org_id,
                    "appointment_id": appt.id,
                    "error": str(exc)[:200],
                }
            },
        )
        return "error"

    tz_name = getattr(org, "timezone", None) or "UTC"
    message = notification_service.render_crm_reschedule_text(new_scheduled_at_utc, tz_name)
    try:
        await notification_service.send_customer_message(org, customer, message)
    except Exception as exc:
        logger.warning(
            "CRM sync reschedule notify failed",
            extra={
                "extra_data": {
                    "event": "crm_sync_reschedule_notify_failed",
                    "org_id": org_id,
                    "appointment_id": appt.id,
                    "error_type": type(exc).__name__,
                }
            },
        )

    logger.info(
        "CRM sync rescheduled appointment",
        extra={"extra_data": {"event": "crm_sync_reschedule", "org_id": org_id, "appointment_id": appt.id}},
    )
    return "synced"


async def sync_org_crm_cancellations(session: AsyncSession, org_id: int) -> dict[str, Any]:
    org = await session.get(Organization, org_id)
    if org is None:
        raise ValueError("Organization not found")
    if not _crm_sync_eligible(org):
        return {"org_id": org_id, "skipped": True, "reason": "crm_not_configured", "synced": 0}

    if not org_bot_operational(org):
        return {"org_id": org_id, "skipped": True, "reason": "org_not_operational", "synced": 0}

    provider = get_crm_provider(org)
    now = datetime.now(timezone.utc)

    if resolve_crm_provider_key(org) == "macdent":
        # MacDent bookings are real zapis records (confirmed live 2026-08-29 —
        # see MacDentProvider.book_appointment), which ARE covered by MacDent's
        # webhook events (onCreate/onChange/onRemove) — unlike the old
        # appointment.send заявки this used to create. Cancellations arrive
        # in real time via bot/api/macdent_webhook.py -> apply_crm_cancellation
        # instead of a poll here. zapis.find's date filter is also confirmed
        # non-functional (see docs/macdent-api-reference.md), so there is no
        # safe polling fallback to fall back to even if we wanted one.
        return {"org_id": org_id, "skipped": True, "reason": "handled_via_webhook", "synced": 0}

    cancelled_status = str(
        getattr(provider, "cancelled_status_value", None) or "cancelled"
    ).strip().lower()

    watermark = _parse_watermark(org)
    since = watermark or (now - timedelta(hours=CRM_SYNC_LOOKBACK_HOURS))
    since_iso = since.astimezone(timezone.utc).isoformat()

    synced = 0
    skipped = 0
    errors = 0

    try:
        snapshots = await provider.list_recent_appointments(since_iso=since_iso)
    except Exception as exc:
        logger.warning(
            "CRM appointment sync list failed",
            extra={
                "extra_data": {
                    "event": "crm_sync_list_failed",
                    "org_id": org_id,
                    "error": str(exc)[:200],
                }
            },
        )
        raise

    for snapshot in snapshots:
        if snapshot.status != cancelled_status:
            skipped += 1
            continue

        appt = await get_appointment_by_crm_id(session, org_id, snapshot.crm_appointment_id)
        if appt is None:
            skipped += 1
            continue

        outcome = await apply_crm_cancellation(session, org, org_id, appt)
        if outcome == "synced":
            synced += 1
        elif outcome == "error":
            errors += 1
        else:
            skipped += 1

    _set_sync_watermark(org, now)
    await session.flush()

    return {
        "org_id": org_id,
        "skipped": False,
        "synced": synced,
        "skipped_snapshots": skipped,
        "errors": errors,
        "since_iso": since_iso,
    }

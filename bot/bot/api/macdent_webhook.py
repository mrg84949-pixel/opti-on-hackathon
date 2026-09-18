"""Inbound webhook receiver for MacDent CRM (onCreate/onChange/onRemove).

Real event shape confirmed live 2026-08-29 via webhook.send_debug (see
docs/macdent-api-reference.md):

    {"eventType": "CREATE"|"CHANGE"|"REMOVE", "objectType": "zapis"|...,
     "objectId": "<id>", "eventData": {...}, "createdAt": "...", ...}

Two cases are handled for `objectType == "zapis"`:
  - `eventType == "REMOVE"` — the unambiguous signal that a real calendar
    booking is gone, regardless of *why* (staff cancelled it in MacDent,
    patient called in, a zapis.remove from our own tooling, etc.). Maps onto
    crm_appointment_sync_service.apply_crm_cancellation, the same
    cancel+notify path the (now-retired for macdent) polling sync used.
  - `eventType == "CHANGE"` — compares `eventData.start` (MacDent's own local
    clinic time, same convention book_appointment() writes with) against our
    stored `scheduled_at`; if it actually moved, treats it as a reschedule
    the clinic made directly in MacDent and mirrors it via
    crm_appointment_sync_service.apply_crm_reschedule — no re-confirmation
    requested, the clinic already acted on their own system.

We deliberately do NOT interpret `eventData.status` for either case — its
encoding for `zapis` objects (unlike `appointment`'s confirmed 0/1/2) was
never verified, only the eventType itself is trustworthy.

`CREATE` and any other objectType are logged (shape only, see
_safe_shape_preview) but not acted on — we don't create/modify local
appointments from a raw CRM push without a concrete need for it.

Security: MacDent's webhook.set has no way to attach a custom auth header,
so an unguessable path segment (macdent_webhook_secret) is the only guard.
Always returns HTTP 200 once the secret matches, even on internal errors
below that point — MacDent retries every 2 minutes for up to 20 attempts on
anything else, and a real processing bug should be fixed and reconciled
manually, not amplified into a retry storm.
"""

from __future__ import annotations

from datetime import timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.crm.macdent import _parse_macdent_dt
from bot.db.database import AsyncSessionLocal
from bot.db.models import Organization
from bot.logging_config import get_logger
from bot.services import crm_appointment_sync_service

logger = get_logger(__name__)

router = APIRouter()

_PREVIEW_MAX_CHARS = 500


async def _resolve_macdent_org(session: AsyncSession) -> Organization | None:
    stmt = select(Organization).where(Organization.crm_provider == "macdent")
    orgs = (await session.execute(stmt)).scalars().all()
    if len(orgs) != 1:
        logger.warning(
            "MacDent webhook: expected exactly one macdent org, found %d",
            len(orgs),
            extra={"extra_data": {"event": "macdent_webhook_org_ambiguous", "count": len(orgs)}},
        )
        return None
    return orgs[0]


def _safe_shape_preview(payload: Any) -> dict[str, Any]:
    """Never assume the body is PII-free — only surface structure, truncated."""
    if isinstance(payload, dict):
        return {
            "top_level_keys": sorted(payload.keys()),
            "preview": str(payload)[:_PREVIEW_MAX_CHARS],
        }
    return {"type": type(payload).__name__, "preview": str(payload)[:_PREVIEW_MAX_CHARS]}


def _macdent_local_to_utc(naive_dt, tz_name: str):
    """MacDent's start/end strings are the org's own local clinic time — same
    convention book_appointment() writes with (local_dt.isoformat(), see
    bot/llm/tools.py _create_appointment_from_draft), not UTC."""
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    return naive_dt.replace(tzinfo=tz).astimezone(timezone.utc)


async def _process_zapis_event(session: AsyncSession, org: Organization, body: dict[str, Any]) -> str | None:
    """Returns an outcome string for logging, or None if this event wasn't
    something we act on (wrong objectType, unhandled eventType, or missing
    fields we need)."""
    event_type = str(body.get("eventType") or "").strip().upper()
    object_type = str(body.get("objectType") or "").strip().lower()
    object_id = body.get("objectId")

    if object_type != "zapis" or not object_id:
        return None

    if event_type == "REMOVE":
        appt = await crm_appointment_sync_service.get_appointment_by_crm_id(session, org.id, str(object_id))
        if appt is None:
            return "unknown_zapis_id"
        return await crm_appointment_sync_service.apply_crm_cancellation(session, org, org.id, appt)

    if event_type == "CHANGE":
        event_data = body.get("eventData")
        raw_start = event_data.get("start") if isinstance(event_data, dict) else None
        if not raw_start:
            return None
        try:
            naive_start = _parse_macdent_dt(str(raw_start))
        except ValueError:
            return None
        appt = await crm_appointment_sync_service.get_appointment_by_crm_id(session, org.id, str(object_id))
        if appt is None:
            return "unknown_zapis_id"
        tz_name = getattr(org, "timezone", None) or "UTC"
        new_scheduled_at_utc = _macdent_local_to_utc(naive_start, tz_name)
        return await crm_appointment_sync_service.apply_crm_reschedule(
            session, org, org.id, appt, new_scheduled_at_utc
        )

    # CREATE and anything else: logged (shape only, see _safe_shape_preview
    # below) but not acted on — we don't create local appointments from a raw
    # CRM push without a concrete need for it.
    return None


@router.post("/macdent/webhook/{secret}")
async def macdent_webhook(secret: str, request: Request) -> dict[str, str]:
    configured = (settings.macdent_webhook_secret or "").strip()
    if not configured:
        logger.warning(
            "MacDent webhook received but macdent_webhook_secret is not configured — rejecting",
            extra={"extra_data": {"event": "macdent_webhook_secret_unset"}},
        )
        return {"status": "ignored"}
    if secret != configured:
        # 404, not 403 — don't confirm to a prober that the path shape is right.
        logger.info(
            "MacDent webhook: path secret mismatch",
            extra={"extra_data": {"event": "macdent_webhook_bad_secret"}},
        )
        return {"status": "ignored"}

    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        body = raw.decode("utf-8", errors="replace")

    shape = _safe_shape_preview(body)

    async with AsyncSessionLocal() as session:
        org = await _resolve_macdent_org(session)
        outcome: str | None = None
        if org is not None and isinstance(body, dict):
            try:
                outcome = await _process_zapis_event(session, org, body)
            except Exception as exc:
                logger.warning(
                    "MacDent webhook processing failed",
                    extra={
                        "extra_data": {
                            "event": "macdent_webhook_process_failed",
                            "org_id": org.id,
                            "error": str(exc)[:200],
                        }
                    },
                )
            else:
                if outcome is not None:
                    await session.commit()

        logger.info(
            "MacDent webhook received",
            extra={
                "extra_data": {
                    "event": "macdent_webhook_received",
                    "org_id": org.id if org else None,
                    "outcome": outcome,
                    **shape,
                }
            },
        )

    return {"status": "ok"}

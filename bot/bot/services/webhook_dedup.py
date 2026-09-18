from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import WebhookSeenEvent
from bot.logging_config import get_logger

logger = get_logger(__name__)

CHANNEL_TELEGRAM = "telegram"
CHANNEL_WA_META = "wa_meta"
CHANNEL_WA_GREEN = "wa_green"


def extract_telegram_event_key(data: dict[str, Any]) -> str | None:
    update_id = data.get("update_id")
    if isinstance(update_id, int):
        return str(update_id)
    return None


def extract_meta_wa_event_key(data: dict[str, Any]) -> str | None:
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for msg in value.get("messages") or []:
                mtype = (msg.get("type") or "").lower()
                if mtype != "text":
                    continue
                msg_id = msg.get("id")
                if msg_id:
                    return str(msg_id)
    return None


def extract_green_wa_event_key(data: dict[str, Any]) -> str | None:
    msg_id = data.get("idMessage")
    if msg_id:
        return str(msg_id)
    return None


async def try_claim_inbound_event(session: AsyncSession, *, channel: str, event_key: str) -> bool:
    """INSERT ON CONFLICT DO NOTHING. True = first delivery, False = duplicate."""
    stmt = (
        insert(WebhookSeenEvent)
        .values(channel=channel, event_key=event_key)
        .on_conflict_do_nothing(constraint="uq_webhook_seen_channel_event")
        .returning(WebhookSeenEvent.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def claim_inbound_event_or_duplicate(
    session: AsyncSession,
    *,
    channel: str,
    event_key: str | None,
) -> bool:
    """Return True to process, False if duplicate. Missing key skips dedup."""
    if not event_key:
        logger.warning(
            "Webhook dedup skipped: no event key",
            extra={"extra_data": {"event": "webhook_dedup_no_key", "channel": channel}},
        )
        return True
    claimed = await try_claim_inbound_event(session, channel=channel, event_key=event_key)
    if not claimed:
        logger.info(
            "Webhook duplicate ignored",
            extra={"extra_data": {"event": "webhook_duplicate", "channel": channel}},
        )
    return claimed

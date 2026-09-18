from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from bot.logging_config import get_logger
from bot.services.outbound_health import note_outbound_send_result
from bot.services.outbound_result import OutboundSendResult

logger = get_logger(__name__)


def handle_reminder_style_outbound(
    target: Any,
    sent_attr: str,
    result: OutboundSendResult,
    *,
    org_id: int,
    log_event: str,
    extra: dict[str, Any] | None = None,
) -> None:
    """Mark sent timestamp on success or auth failure; retry only on transient errors."""
    note_outbound_send_result(org_id, result)
    now = datetime.now(timezone.utc)
    payload = {"org_id": org_id, **(extra or {})}
    if result.ok:
        setattr(target, sent_attr, now)
        return
    if result.auth_failed:
        setattr(target, sent_attr, now)
        payload["channel"] = result.channel
        logger.warning(
            "Outbound auth failure suppressed job retry",
            extra={"extra_data": {**payload, "event": log_event}},
        )

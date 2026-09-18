"""In-process outbound auth health per org channel (POST-03)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bot.services.outbound_result import OutboundSendResult

OUTBOUND_AUTH_FAILURE_TTL_HOURS = 24

_auth_failures: dict[tuple[int, str], datetime] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def record_auth_failure(org_id: int, channel: str) -> None:
    if org_id <= 0 or not channel:
        return
    _auth_failures[(org_id, channel)] = _now()


def clear_channel_health(org_id: int, channel: str) -> None:
    if org_id <= 0 or not channel:
        return
    _auth_failures.pop((org_id, channel), None)


def clear_org_outbound_health(org_id: int) -> None:
    if org_id <= 0:
        return
    for key in list(_auth_failures):
        if key[0] == org_id:
            del _auth_failures[key]


def channel_send_healthy(org_id: int, channel: str, *, now: datetime | None = None) -> bool:
    if org_id <= 0 or not channel:
        return True
    failed_at = _auth_failures.get((org_id, channel))
    if failed_at is None:
        return True
    ref = now or _now()
    if failed_at.tzinfo is None:
        failed_at = failed_at.replace(tzinfo=timezone.utc)
    ttl = timedelta(hours=OUTBOUND_AUTH_FAILURE_TTL_HOURS)
    if ref - failed_at >= ttl:
        _auth_failures.pop((org_id, channel), None)
        return True
    return False


def note_outbound_send_result(org_id: int, result: OutboundSendResult) -> None:
    channel = result.channel
    if not channel or org_id <= 0:
        return
    if result.ok:
        clear_channel_health(org_id, channel)
    elif result.auth_failed:
        record_auth_failure(org_id, channel)


def reset_outbound_health_cache() -> None:
    """Test helper."""
    _auth_failures.clear()

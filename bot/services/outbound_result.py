"""Structured results for tenant outbound sends (Telegram / WhatsApp)."""
from __future__ import annotations

from dataclasses import dataclass


def is_auth_http_status(status: int) -> bool:
    return status in (401, 403)


def telegram_response_auth_failed(status_code: int, body_text: str) -> bool:
    if is_auth_http_status(status_code):
        return True
    if status_code != 400:
        return False
    lowered = (body_text or "").lower()
    return "unauthorized" in lowered or "forbidden" in lowered


@dataclass(frozen=True)
class OutboundSendResult:
    ok: bool
    auth_failed: bool = False
    channel: str | None = None

    def __bool__(self) -> bool:
        return self.ok

    @classmethod
    def success(cls, channel: str | None = None) -> OutboundSendResult:
        return cls(ok=True, auth_failed=False, channel=channel)

    @classmethod
    def skipped(cls, channel: str | None = None) -> OutboundSendResult:
        return cls(ok=False, auth_failed=False, channel=channel)

    @classmethod
    def auth_error(cls, channel: str | None = None) -> OutboundSendResult:
        return cls(ok=False, auth_failed=True, channel=channel)

    @classmethod
    def retryable_error(cls, channel: str | None = None) -> OutboundSendResult:
        return cls(ok=False, auth_failed=False, channel=channel)

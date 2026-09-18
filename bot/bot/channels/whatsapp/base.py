"""Common transport interface for outbound WhatsApp sends.

One interface, swappable providers — Green API today, Meta Cloud already,
a future official-WABA BSP (e.g. YCloud) later — without touching call
sites in bot/services or web/admin_api.py. Mirrors the CRMProvider pattern
in bot/crm/base.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TransportSendResult:
    ok: bool
    error: str | None = None
    auth_failed: bool = False


class WhatsAppTransport(Protocol):
    async def send_text(self, *, chat_id: str, text: str) -> TransportSendResult:
        """Send a plain text message. chat_id shape is transport-specific
        (Green: '<digits>@c.us', Meta: E.164/digits-only phone)."""

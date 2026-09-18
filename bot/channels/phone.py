"""Map channel + user id to stored customer.phone prefix."""
from __future__ import annotations


def channel_phone(channel: str, user_id: str) -> str:
    if channel == "telegram":
        return f"tg:{user_id}"
    if channel == "whatsapp":
        return f"wa:{user_id}"
    return f"web:{user_id or 'guest'}"

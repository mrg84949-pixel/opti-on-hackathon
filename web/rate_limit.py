from __future__ import annotations

from fastapi import Request

from bot.config import settings
from bot.services.rate_limit import RateLimitExceeded, client_ip, enforce_limit

AUTH_EMAIL_WINDOW_SECONDS = 15 * 60


async def enforce_auth_limits(request: Request, *, action: str, identifier: str | None = None) -> None:
    if not settings.rate_limit_enabled:
        return
    ip = client_ip(request)
    await enforce_limit(
        f"auth:ip:{ip}:{action}",
        limit=settings.rate_limit_auth_ip_limit,
        window_seconds=60,
    )
    if identifier:
        ident = identifier.strip().lower()
        if ident:
            await enforce_limit(
                f"auth:email:{action}:{ident}",
                limit=settings.rate_limit_auth_email_limit,
                window_seconds=AUTH_EMAIL_WINDOW_SECONDS,
            )

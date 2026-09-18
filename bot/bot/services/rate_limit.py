from __future__ import annotations

import asyncio
import time

from fastapi import Request

_limiter_instance: InMemoryRateLimiter | None = None


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded; retry after {retry_after}s")


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str, *, limit: int, window_seconds: int) -> int | None:
        if limit <= 0:
            return None
        now = time.monotonic()
        async with self._lock:
            window_start, count = self._store.get(key, (now, 0))
            if now - window_start >= window_seconds:
                window_start = now
                count = 0
            count += 1
            self._store[key] = (window_start, count)
            if count > limit:
                elapsed = now - window_start
                retry_after = max(1, int(window_seconds - elapsed))
                return retry_after
        return None

    def reset_for_tests(self) -> None:
        self._store.clear()


def get_limiter() -> InMemoryRateLimiter:
    global _limiter_instance
    if _limiter_instance is None:
        _limiter_instance = InMemoryRateLimiter()
    return _limiter_instance


def reset_for_tests() -> None:
    get_limiter().reset_for_tests()


def client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


async def enforce_limit(key: str, *, limit: int, window_seconds: int) -> None:
    from bot.config import settings

    if not settings.rate_limit_enabled:
        return
    retry_after = await get_limiter().check(key, limit=limit, window_seconds=window_seconds)
    if retry_after is not None:
        raise RateLimitExceeded(retry_after)

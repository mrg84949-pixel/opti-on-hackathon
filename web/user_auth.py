from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Header, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db.models import UserAccount, UserSession

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_TTL_HOURS = 8


def _auth_secret() -> str:
    return settings.user_auth_secret


def auth_secret_configured() -> bool:
    secret = _auth_secret().strip()
    return bool(secret and secret != "change-me")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email))


def hash_secret(raw: str) -> str:
    secret = _auth_secret()
    return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_password(password: str) -> str:
    return f"sha256${hash_secret(password)}"


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash.startswith("sha256$"):
        return False
    expected = stored_hash.split("$", 1)[1]
    return hmac.compare_digest(expected, hash_secret(password))


def issue_token() -> str:
    return secrets.token_urlsafe(32)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def create_session(session: AsyncSession, user_id: int) -> tuple[str, datetime]:
    session_id = issue_token()
    expires_at = now_utc() + timedelta(hours=SESSION_TTL_HOURS)
    session.add(UserSession(user_id=user_id, session_id=session_id, expires_at=expires_at))
    await session.flush()
    return session_id, expires_at


async def rotate_session(session: AsyncSession, old_session: UserSession) -> tuple[str, datetime]:
    await session.delete(old_session)
    return await create_session(session, old_session.user_id)


async def clear_session(session: AsyncSession, session_id: str) -> None:
    await session.execute(delete(UserSession).where(UserSession.session_id == session_id))


async def require_user_auth(
    session: AsyncSession,
    x_user_session: str | None = Header(default=None),
) -> UserAccount:
    if not x_user_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    result = await session.execute(select(UserSession).where(UserSession.session_id == x_user_session))
    user_session = result.scalar_one_or_none()
    if not user_session or user_session.expires_at <= now_utc():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    user = await session.get(UserAccount, user_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return user

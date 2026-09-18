from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.db.database import AsyncSessionLocal
from bot.db.models import Admin, AdminSession
from web.user_auth import hash_password, issue_token, now_utc, verify_password

class AmbiguousAdminLoginError(Exception):
    """Multiple admins share login; caller must pass org_id."""


__all__ = [
    "ADMIN_SESSION_TTL_HOURS",
    "AdminAuth",
    "AmbiguousAdminLoginError",
    "authenticate_admin_login",
    "clear_admin_session",
    "create_admin_session",
    "get_admin_auth",
    "hash_password",
    "require_admin_session",
    "require_ops_token",
    "resolve_org_id_from_auth",
    "verify_password",
]

ADMIN_SESSION_TTL_HOURS = 8


@dataclass(frozen=True)
class AdminAuth:
    mode: Literal["token", "session"]
    org_id: int | None
    admin_id: int | None


def _admin_session_header(x_admin_session: str | None = Header(default=None, alias="X-Admin-Session")) -> str | None:
    raw = (x_admin_session or "").strip()
    return raw or None


def _authorization_header(authorization: str | None = Header(default=None)) -> str | None:
    return authorization


async def _load_admin_by_session(session: AsyncSession, session_id: str) -> Admin | None:
    stmt = (
        select(AdminSession)
        .where(AdminSession.session_id == session_id)
        .options(selectinload(AdminSession.admin))
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at < now_utc():
        await session.delete(row)
        await session.flush()
        return None
    return row.admin


async def create_admin_session(session: AsyncSession, admin_id: int) -> tuple[str, datetime]:
    session_id = issue_token()
    expires_at = now_utc() + timedelta(hours=ADMIN_SESSION_TTL_HOURS)
    session.add(AdminSession(admin_id=admin_id, session_id=session_id, expires_at=expires_at))
    await session.flush()
    return session_id, expires_at


async def clear_admin_session(session: AsyncSession, session_id: str) -> None:
    await session.execute(delete(AdminSession).where(AdminSession.session_id == session_id))


async def authenticate_admin_login(
    login: str,
    password: str,
    *,
    org_id: int | None = None,
) -> Admin | None:
    login_norm = login.strip()
    if not login_norm or not password:
        return None
    async with AsyncSessionLocal() as session:
        if org_id is not None:
            admin = (
                await session.execute(
                    select(Admin).where(Admin.org_id == org_id, Admin.login == login_norm)
                )
            ).scalar_one_or_none()
            if admin is None or not verify_password(password, admin.password_hash):
                return None
            return admin

        rows = (
            await session.execute(select(Admin).where(Admin.login == login_norm))
        ).scalars().all()
        matches = [a for a in rows if verify_password(password, a.password_hash)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            return None
        raise AmbiguousAdminLoginError()


async def resolve_admin_auth(
    authorization: str | None,
    x_admin_session: str | None,
) -> AdminAuth:
    if x_admin_session:
        async with AsyncSessionLocal() as session:
            admin = await _load_admin_by_session(session, x_admin_session)
            if admin is not None:
                await session.commit()
                return AdminAuth(mode="session", org_id=admin.org_id, admin_id=admin.id)

    expected = (settings.admin_api_token or "").strip()
    if expected and authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token == expected:
            return AdminAuth(mode="token", org_id=None, admin_id=None)

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


async def get_admin_auth(
    authorization: str | None = Depends(_authorization_header),
    x_admin_session: str | None = Depends(_admin_session_header),
) -> AdminAuth:
    return await resolve_admin_auth(authorization, x_admin_session)


def require_admin_session(auth: AdminAuth) -> AdminAuth:
    if auth.mode != "session" or auth.admin_id is None or auth.org_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin session required",
        )
    return auth


def require_ops_token(auth: AdminAuth) -> AdminAuth:
    if auth.mode != "token":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Ops token required",
        )
    return auth


def resolve_org_id_from_auth(
    auth: AdminAuth,
    x_org_id: int | None = None,
) -> int:
    if auth.mode == "session" and auth.org_id is not None:
        return auth.org_id
    if x_org_id is not None and x_org_id > 0:
        return x_org_id
    return settings.default_org_id

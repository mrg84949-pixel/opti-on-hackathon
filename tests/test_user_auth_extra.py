from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import web.user_auth as user_auth


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    def __init__(self, *, execute_results=None, get_results=None):
        self._execute_results = list(execute_results or [])
        self._get_results = dict(get_results or {})
        self.added = []
        self.deleted = []
        self.executed = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def delete(self, obj):
        self.deleted.append(obj)

    async def execute(self, stmt):
        self.executed.append(stmt)
        if not self._execute_results:
            return None
        return self._execute_results.pop(0)

    async def get(self, model, key):
        return self._get_results.get((model, key))


def test_auth_secret_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("USER_AUTH_SECRET", raising=False)
    assert user_auth.auth_secret_configured() is False
    monkeypatch.setenv("USER_AUTH_SECRET", "change-me")
    assert user_auth.auth_secret_configured() is False
    monkeypatch.setenv("USER_AUTH_SECRET", "super-secret")
    assert user_auth.auth_secret_configured() is True


@pytest.mark.asyncio
async def test_create_rotate_and_clear_session(monkeypatch: pytest.MonkeyPatch):
    session = _FakeSession()
    monkeypatch.setattr(user_auth, "issue_token", lambda: "session-token")
    monkeypatch.setattr(user_auth, "now_utc", lambda: datetime(2030, 1, 1, tzinfo=timezone.utc))

    session_id, expires_at = await user_auth.create_session(session, 7)
    assert session_id == "session-token"
    assert expires_at == datetime(2030, 1, 1, tzinfo=timezone.utc) + timedelta(hours=user_auth.SESSION_TTL_HOURS)
    assert session.added[0].user_id == 7

    old_session = SimpleNamespace(user_id=7)
    rotated_session_id, _ = await user_auth.rotate_session(session, old_session)
    assert rotated_session_id == "session-token"
    assert session.deleted == [old_session]

    await user_auth.clear_session(session, "session-token")
    assert session.executed


@pytest.mark.asyncio
async def test_require_user_auth_branches(monkeypatch: pytest.MonkeyPatch):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(user_auth, "now_utc", lambda: now)

    with pytest.raises(HTTPException) as missing:
        await user_auth.require_user_auth(_FakeSession(), None)
    assert missing.value.status_code == 401

    expired_session = _FakeSession(
        execute_results=[_ScalarOneOrNoneResult(SimpleNamespace(user_id=1, expires_at=now))]
    )
    with pytest.raises(HTTPException) as expired:
        await user_auth.require_user_auth(expired_session, "expired")
    assert expired.value.status_code == 401

    inactive_user_session = _FakeSession(
        execute_results=[_ScalarOneOrNoneResult(SimpleNamespace(user_id=1, expires_at=now + timedelta(hours=1)))],
        get_results={(user_auth.UserAccount, 1): SimpleNamespace(id=1, is_active=False)},
    )
    with pytest.raises(HTTPException) as inactive:
        await user_auth.require_user_auth(inactive_user_session, "inactive")
    assert inactive.value.status_code == 401

    active_user = SimpleNamespace(id=2, is_active=True)
    valid_session = _FakeSession(
        execute_results=[_ScalarOneOrNoneResult(SimpleNamespace(user_id=2, expires_at=now + timedelta(hours=1)))],
        get_results={(user_auth.UserAccount, 2): active_user},
    )
    assert await user_auth.require_user_auth(valid_session, "valid") is active_user

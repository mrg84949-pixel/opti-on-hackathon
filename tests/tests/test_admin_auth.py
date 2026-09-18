from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.db.models import Admin, AdminSession
from web import admin_auth


class _FakeSession:
    def __init__(self):
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.committed = False

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def delete(self, obj: object) -> None:
        self.deleted.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _FakeSessionManager:
    def __init__(self, session: _FakeSession):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_create_admin_session(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeSession()
    monkeypatch.setattr(admin_auth, "AsyncSessionLocal", lambda: _FakeSessionManager(fake))
    async with _FakeSessionManager(fake) as session:
        sid, exp = await admin_auth.create_admin_session(session, 1)
    assert len(sid) > 10
    assert exp > datetime.now(timezone.utc)
    assert len(fake.added) == 1
    assert isinstance(fake.added[0], AdminSession)


@pytest.mark.asyncio
async def test_authenticate_admin_login_wrong_password(monkeypatch: pytest.MonkeyPatch):
    admin = Admin(id=1, login="owner", password_hash="sha256$dead", org_id=1)

    class _Scalars:
        def all(self):
            return [admin]

    class _Result:
        def scalars(self):
            return _Scalars()

    class _Db:
        async def execute(self, _stmt):
            return _Result()

    monkeypatch.setattr(admin_auth, "AsyncSessionLocal", lambda: _FakeSessionManager(_Db()))
    monkeypatch.setattr(admin_auth, "verify_password", lambda _p, _h: False)
    assert await admin_auth.authenticate_admin_login("owner", "bad") is None

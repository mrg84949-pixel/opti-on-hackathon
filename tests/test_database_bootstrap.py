from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.db.database as database
import main


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _FakeSession:
    def __init__(self, *, execute_results=None):
        self._execute_results = list(execute_results or [])
        self.added = []
        self.committed = False
        self.refreshed = False

    async def execute(self, _stmt):
        if not self._execute_results:
            raise AssertionError("Unexpected execute() call")
        return self._execute_results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added.extend(objs)

    async def commit(self):
        self.committed = True

    async def refresh(self, _obj):
        self.refreshed = True


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self):
        self.executed = []
        self.run_sync_calls = []

    async def execute(self, stmt):
        self.executed.append(str(stmt))

    async def run_sync(self, fn):
        self.run_sync_calls.append(fn)
        return fn("sync-connection")


class _FakeEngineBegin:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    def begin(self):
        return _FakeEngineBegin(self.conn)


@pytest.mark.asyncio
async def test_get_db_yields_session(monkeypatch: pytest.MonkeyPatch):
    session = object()
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _FakeSessionManager(session))
    yielded = []
    async for item in database.get_db():
        yielded.append(item)
    assert yielded == [session]


@pytest.mark.asyncio
async def test_ensure_billing_and_logs_schema_executes_all_ddl(monkeypatch: pytest.MonkeyPatch):
    conn = _FakeConn()
    monkeypatch.setattr(database, "engine", _FakeEngine(conn))
    await database._ensure_billing_and_logs_schema()
    assert any("billing_paid_until" in stmt for stmt in conn.executed)
    assert any("bot_interaction_logs" in stmt for stmt in conn.executed)
    assert any("whatsapp_provider" in stmt for stmt in conn.executed)
    assert any("whatsapp_broadcast_locked" in stmt for stmt in conn.executed)


@pytest.mark.asyncio
async def test_seed_demo_org_and_service_catalog_branches(monkeypatch: pytest.MonkeyPatch):
    existing_org_session = _FakeSession(execute_results=[_ScalarOneOrNoneResult(SimpleNamespace(id=1))])
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _FakeSessionManager(existing_org_session))
    await database._seed_demo_organization_if_empty()
    assert existing_org_session.added == []

    empty_org_session = _FakeSession(execute_results=[_ScalarOneOrNoneResult(None)])
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _FakeSessionManager(empty_org_session))
    await database._seed_demo_organization_if_empty()
    assert len(empty_org_session.added) == 1
    assert empty_org_session.committed is True

    existing_catalog_session = _FakeSession(execute_results=[_ScalarOneOrNoneResult(SimpleNamespace(id=1))])
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _FakeSessionManager(existing_catalog_session))
    await database._seed_service_catalog_if_empty()
    assert existing_catalog_session.added == []

    empty_catalog_session = _FakeSession(execute_results=[_ScalarOneOrNoneResult(None)])
    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _FakeSessionManager(empty_catalog_session))
    await database._seed_service_catalog_if_empty()
    assert len(empty_catalog_session.added) == 3
    assert any(item.slug == "optibot-trial" for item in empty_catalog_session.added)
    assert empty_catalog_session.committed is True


@pytest.mark.asyncio
async def test_init_db_orchestrates_create_all_and_seeds(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SKIP_METADATA_CREATE_ALL", raising=False)
    order = []
    conn = _FakeConn()
    monkeypatch.setattr(database, "engine", _FakeEngine(conn))
    monkeypatch.setattr(database.Base.metadata, "create_all", lambda sync_conn: order.append(("create_all", sync_conn)))

    async def fake_ensure():
        order.append("ensure")

    async def fake_seed_org():
        order.append("seed_org")

    async def fake_seed_catalog():
        order.append("seed_catalog")

    monkeypatch.setattr(database, "_ensure_billing_and_logs_schema", fake_ensure)
    monkeypatch.setattr(database, "_seed_demo_organization_if_empty", fake_seed_org)
    monkeypatch.setattr(database, "_seed_service_catalog_if_empty", fake_seed_catalog)

    await database.init_db()
    assert order == [("create_all", "sync-connection"), "ensure", "seed_org", "seed_catalog"]


@pytest.mark.asyncio
async def test_init_db_skips_create_all_when_skip_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SKIP_METADATA_CREATE_ALL", "1")
    order = []
    conn = _FakeConn()
    monkeypatch.setattr(database, "engine", _FakeEngine(conn))
    monkeypatch.setattr(database.Base.metadata, "create_all", lambda sync_conn: order.append(("create_all", sync_conn)))

    async def fake_ensure():
        order.append("ensure")

    async def fake_seed_org():
        order.append("seed_org")

    async def fake_seed_catalog():
        order.append("seed_catalog")

    monkeypatch.setattr(database, "_ensure_billing_and_logs_schema", fake_ensure)
    monkeypatch.setattr(database, "_seed_demo_organization_if_empty", fake_seed_org)
    monkeypatch.setattr(database, "_seed_service_catalog_if_empty", fake_seed_catalog)

    await database.init_db()
    assert order == ["ensure", "seed_org", "seed_catalog"]
    assert conn.run_sync_calls == []


@pytest.mark.asyncio
async def test_main_lifespan_runs_startup_and_shutdown(monkeypatch: pytest.MonkeyPatch):
    calls = []

    async def fake_init_db():
        calls.append("init_db")

    def fake_start_scheduler():
        calls.append("start_scheduler")

    def fake_stop_scheduler():
        calls.append("stop_scheduler")

    monkeypatch.setattr(main, "init_db", fake_init_db)
    monkeypatch.setattr(main, "start_scheduler", fake_start_scheduler)
    monkeypatch.setattr(main, "stop_scheduler", fake_stop_scheduler)

    async with main.lifespan(main.app):
        calls.append("inside")

    assert calls == ["init_db", "start_scheduler", "inside", "stop_scheduler"]

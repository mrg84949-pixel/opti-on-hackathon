from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import alembic


class _Tx:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeAsyncConnection:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run_sync(self, fn):
        self.calls.append("run_sync")
        fn("sync-connection")


class _FakeAsyncEngine:
    def __init__(self, calls):
        self.calls = calls

    def connect(self):
        self.calls.append("connect")
        return _FakeAsyncConnection(self.calls)

    async def dispose(self):
        self.calls.append("dispose")


def test_alembic_env_offline_import_and_async_online(monkeypatch):
    calls: list[object] = []
    fake_config = SimpleNamespace(
        config_file_name=None,
        config_ini_section="alembic",
        get_main_option=lambda key: "postgresql+asyncpg://example/db",
        get_section=lambda section, default=None: {},
    )

    fake_context = SimpleNamespace(
        config=fake_config,
        configure=lambda **kwargs: calls.append(("configure", kwargs)),
        begin_transaction=lambda: _Tx(),
        run_migrations=lambda: calls.append("run_migrations"),
        is_offline_mode=lambda: True,
    )
    monkeypatch.setattr(alembic, "context", fake_context)

    env_path = Path(__file__).resolve().parents[1] / "alembic" / "env.py"
    spec = importlib.util.spec_from_file_location("test_alembic_env_module", env_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert any(item[0] == "configure" for item in calls if isinstance(item, tuple))
    assert "run_migrations" in calls

    async_calls: list[str] = []
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://example/db")
    monkeypatch.setattr(module, "create_async_engine", lambda url, poolclass=None: _FakeAsyncEngine(async_calls))
    module.run_migrations_online()
    assert async_calls == ["connect", "run_sync", "dispose"]

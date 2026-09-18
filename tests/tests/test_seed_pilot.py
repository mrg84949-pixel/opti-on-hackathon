from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("seed_pilot", ROOT / "scripts" / "seed_pilot.py")
assert _spec and _spec.loader
seed_pilot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_pilot)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeCustomerSession:
    """Minimal session: one smoke customer per org."""

    def __init__(self):
        self.customer = None
        self.added: list = []

    async def execute(self, _stmt):
        return _ScalarResult(self.customer)

    def add(self, obj):
        self.added.append(obj)
        if self.customer is None:
            obj.id = 101
            obj.phone = seed_pilot.PILOT_SMOKE_PHONE
            self.customer = obj

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_ensure_smoke_customer_idempotent():
    session = _FakeCustomerSession()
    first = await seed_pilot._ensure_smoke_customer(session, 1)
    second = await seed_pilot._ensure_smoke_customer(session, 1)
    assert first.id == 101
    assert second.id == 101
    assert first.phone == seed_pilot.PILOT_SMOKE_PHONE
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_ensure_smoke_customer_migrates_legacy_phone():
    legacy = SimpleNamespace(id=42, phone=seed_pilot.LEGACY_PILOT_SMOKE_PHONE)
    calls = {"n": 0}

    class _Session:
        async def execute(self, _stmt):
            calls["n"] += 1
            if calls["n"] == 1:
                return _ScalarResult(None)
            if calls["n"] == 2:
                return _ScalarResult(legacy)
            return _ScalarResult(None)

        async def flush(self):
            return None

    customer = await seed_pilot._ensure_smoke_customer(_Session(), 1)
    assert customer.id == 42
    assert customer.phone == seed_pilot.PILOT_SMOKE_PHONE


@pytest.mark.asyncio
async def test_ensure_pilot_log_finds_legacy_external_id():
    legacy_log = SimpleNamespace(
        id=7,
        org_id=1,
        external_user_id="pilot-smoke",
    )
    session = SimpleNamespace(added=[], flushed=False)

    async def execute(_stmt):
        return _ScalarResult(legacy_log)

    async def flush():
        session.flushed = True

    session.execute = execute
    session.flush = flush
    session.add = lambda obj: session.added.append(obj)

    created = await seed_pilot._ensure_pilot_log(
        session, 1, seed_pilot.PILOT_SMOKE_PHONE
    )
    assert created is False
    assert legacy_log.external_user_id == "900000001"
    assert session.added == []
    assert session.flushed is True


@pytest.mark.asyncio
async def test_ensure_pilot_log_idempotent():
    existing_log = SimpleNamespace(
        id=8,
        org_id=1,
        external_user_id="900000001",
    )
    session = SimpleNamespace(added=[], flushed=False)

    async def execute(_stmt):
        return _ScalarResult(existing_log)

    async def flush():
        session.flushed = True

    session.execute = execute
    session.flush = flush
    session.add = lambda obj: session.added.append(obj)

    first = await seed_pilot._ensure_pilot_log(session, 1, seed_pilot.PILOT_SMOKE_PHONE)
    second = await seed_pilot._ensure_pilot_log(session, 1, seed_pilot.PILOT_SMOKE_PHONE)
    assert first is False
    assert second is False
    assert session.added == []


@pytest.mark.asyncio
async def test_seed_pilot_missing_org():
    class _Session:
        async def get(self, _model, _oid):
            return None

    with pytest.raises(ValueError, match="not found"):
        await seed_pilot.seed_pilot(_Session(), 99, login="owner", password="secret")


@pytest.mark.asyncio
async def test_seed_pilot_result_shape(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(id=1, name="Pilot Clinic")
    admin = SimpleNamespace(id=10, login="owner")
    customer = SimpleNamespace(id=55, phone=seed_pilot.PILOT_SMOKE_PHONE)

    class _Session:
        async def get(self, _model, oid):
            return org if oid == 1 else None

    async def fake_admin(_session, org_id, *, login, password):
        assert org_id == 1
        assert login == "owner"
        return admin

    async def fake_customer(_session, org_id):
        return customer

    async def fake_log(_session, org_id, phone):
        assert phone == seed_pilot.PILOT_SMOKE_PHONE
        return True

    async def fake_services(_session, org_id):
        return True

    monkeypatch.setattr(seed_pilot, "_ensure_admin", fake_admin)
    monkeypatch.setattr(seed_pilot, "_ensure_smoke_customer", fake_customer)
    monkeypatch.setattr(seed_pilot, "_ensure_pilot_log", fake_log)
    monkeypatch.setattr(seed_pilot, "_ensure_pilot_services", fake_services)

    result = await seed_pilot.seed_pilot(_Session(), 1, login="owner", password="secret")
    assert result["org_id"] == 1
    assert result["customer_id"] == 55
    assert result["customer_phone"] == seed_pilot.PILOT_SMOKE_PHONE
    assert result["admin_login"] == "owner"
    assert result["conversation_log_created"] is True
    assert result["services_seeded"] is True

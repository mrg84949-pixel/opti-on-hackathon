from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.services import customer_service


@pytest.mark.asyncio
async def test_mute_for_human_handoff_sets_mute(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=5, org_id=1, muted_until=None)
    calls = {"set_mute": 0}

    async def fake_get(session, customer_id, org_id):
        assert customer_id == 5
        assert org_id == 1
        return customer

    async def fake_set_mute(session, customer_id, org_id, **kwargs):
        calls["set_mute"] += 1
        customer.muted_until = datetime.now(timezone.utc) + timedelta(days=7)
        return {"customer_id": customer_id}

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)
    monkeypatch.setattr(customer_service, "set_mute", fake_set_mute)

    applied = await customer_service.mute_for_human_handoff(None, 5, 1)
    assert applied is True
    assert calls["set_mute"] == 1
    assert customer.muted_until is not None


@pytest.mark.asyncio
async def test_mute_for_human_handoff_skips_already_muted(monkeypatch: pytest.MonkeyPatch):
    now = datetime.now(timezone.utc)
    customer = SimpleNamespace(id=5, org_id=1, muted_until=now + timedelta(days=3))

    async def fake_get(session, customer_id, org_id):
        return customer

    async def fake_set_mute(*args, **kwargs):
        raise AssertionError("set_mute should not be called for already muted customer")

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)
    monkeypatch.setattr(customer_service, "set_mute", fake_set_mute)

    applied = await customer_service.mute_for_human_handoff(None, 5, 1)
    assert applied is False


@pytest.mark.asyncio
async def test_mute_for_human_handoff_missing_customer(monkeypatch: pytest.MonkeyPatch):
    async def fake_get(session, customer_id, org_id):
        return None

    async def fake_set_mute(*args, **kwargs):
        raise AssertionError("set_mute should not be called when customer is missing")

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)
    monkeypatch.setattr(customer_service, "set_mute", fake_set_mute)

    applied = await customer_service.mute_for_human_handoff(None, 99, 1)
    assert applied is False

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services import customer_service


def test_normalize_customer_phone_telegram():
    assert customer_service.normalize_customer_phone(telegram_id="12345") == "tg:12345"
    assert customer_service.normalize_customer_phone(phone="tg:99") == "tg:99"


def test_normalize_customer_phone_whatsapp():
    assert customer_service.normalize_customer_phone(whatsapp_id="+79991234567") == "wa:79991234567"


@pytest.mark.asyncio
async def test_reset_customer_for_org_clears_state(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=5,
        org_id=1,
        phone="tg:12345",
        name="Alice",
        dialog_context={"pending_booking": {"service": "X"}},
        muted_until="2099-01-01",
        disable_reminders=True,
    )
    cleared: list[tuple[str, str, int]] = []

    class Session:
        async def execute(self, stmt):
            sql = str(stmt).lower()
            if "bot_interaction_log" in sql:
                return SimpleNamespace(rowcount=2)
            if "appointment" in sql:
                return SimpleNamespace(rowcount=3)
            raise AssertionError(f"unexpected stmt: {stmt}")

        async def flush(self):
            return None

    async def fake_get(_session, customer_id, org_id):
        assert customer_id == 5 and org_id == 1
        return customer

    def fake_clear_session(*, channel, user_id, org_id):
        cleared.append((channel, user_id, org_id))

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)
    monkeypatch.setattr(
        "bot.llm.llm_engine.clear_in_memory_session",
        fake_clear_session,
    )

    result = await customer_service.reset_customer_for_org(Session(), 5, 1)

    assert result == {
        "customer_id": 5,
        "org_id": 1,
        "phone": "tg:12345",
        "logs_removed": 2,
        "appointments_removed": 3,
        "name_cleared": True,
        "llm_session_cleared": True,
    }
    assert customer.dialog_context == {}
    assert customer.muted_until is None
    assert customer.disable_reminders is False
    assert customer.name is None
    assert cleared == [("telegram", "12345", 1)]


@pytest.mark.asyncio
async def test_reset_customer_for_org_keep_name(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=7,
        org_id=1,
        phone="wa:7999",
        name="Bob",
        dialog_context={"dialog_mode": "manage"},
        muted_until=None,
        disable_reminders=False,
    )

    class Session:
        async def execute(self, _stmt):
            return SimpleNamespace(rowcount=0)

        async def flush(self):
            return None

    monkeypatch.setattr(
        customer_service,
        "get_customer_for_org",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(
        "bot.llm.llm_engine.clear_in_memory_session",
        lambda **_kwargs: None,
    )

    result = await customer_service.reset_customer_for_org(
        Session(), 7, 1, clear_name=False, clear_llm_session=False
    )

    assert result is not None
    assert result["name_cleared"] is False
    assert result["llm_session_cleared"] is False
    assert customer.name == "Bob"


@pytest.mark.asyncio
async def test_reset_customer_for_org_missing_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        customer_service,
        "get_customer_for_org",
        AsyncMock(return_value=None),
    )
    result = await customer_service.reset_customer_for_org(SimpleNamespace(), 99, 1)
    assert result is None

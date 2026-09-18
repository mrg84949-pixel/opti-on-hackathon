from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.llm import context_summary as cs
from bot.llm.context import TurnContext
from bot.llm.tools import tool_by_name


def test_validate_summary_length():
    with pytest.raises(ValueError):
        cs.validate_summary_text("short")
    ok = cs.validate_summary_text("x" * 25)
    assert len(ok) == 25


def test_build_post_booking_summary():
    text = cs.build_post_booking_summary(
        customer_name="Ali",
        service="Консультация",
        when_label="01.06.2026 10:00",
        appointment_id=42,
        timezone_name="UTC",
    )
    assert "Ali" in text
    assert "42" in text
    assert len(text) >= cs.MIN_SUMMARY_LEN


@pytest.mark.asyncio
async def test_compress_context_tool_persists(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=10, dialog_context={})
    fake_session = SimpleNamespace(customer=customer)

    class _Mgr:
        async def __aenter__(self):
            return fake_session

        async def __aexit__(self, *args):
            return False

    committed = {"ok": False}

    async def commit():
        committed["ok"] = True

    fake_session.commit = commit
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", lambda: _Mgr())

    async def fake_load(_session, _cid):
        return customer

    monkeypatch.setattr("bot.llm.tools._load_customer", fake_load)

    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    result = await tool_by_name(ctx, "compress_context")(
        "Клиент Ali, услуга консультация, запись на завтра."
    )
    assert "Контекст сжат" in result
    assert cs.CONTEXT_SUMMARY_KEY in customer.dialog_context
    assert committed["ok"]


@pytest.mark.asyncio
async def test_get_customer_context_empty(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=10, dialog_context={})

    class _Mgr:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", lambda: _Mgr())
    async def fake_load(_session, _cid):
        return customer

    monkeypatch.setattr("bot.llm.tools._load_customer", fake_load)
    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    result = await tool_by_name(ctx, "get_customer_context")()
    assert "пуст" in result

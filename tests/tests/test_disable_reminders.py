from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.llm.context import TurnContext
from bot.llm.tools import DIALOG_MODE_BOOKING, DIALOG_MODE_MANAGE, make_tools, tool_by_name
from bot.services import customer_service


class _FakeSession:
    def __init__(self, *, customer=None):
        self.customer = customer
        self.committed = False

    async def get(self, model, key):
        if getattr(model, "__name__", "") == "Customer":
            return self.customer
        return None

    async def commit(self):
        self.committed = True


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_reminders_disabled_helper():
    assert customer_service.reminders_disabled(SimpleNamespace(disable_reminders=True)) is True
    assert customer_service.reminders_disabled(SimpleNamespace(disable_reminders=False)) is False
    assert customer_service.reminders_disabled(SimpleNamespace()) is False


@pytest.mark.asyncio
async def test_set_reminders_disabled_org_scoped(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=5, org_id=1, disable_reminders=False)

    async def fake_get(session, customer_id, org_id):
        if customer_id == 5 and org_id == 1:
            return customer
        return None

    monkeypatch.setattr(customer_service, "get_customer_for_org", fake_get)
    session = SimpleNamespace()
    result = await customer_service.set_reminders_disabled(session, 5, 1, disabled=True)
    assert result == {"customer_id": 5, "disable_reminders": True}
    assert customer.disable_reminders is True

    missing = await customer_service.set_reminders_disabled(session, 5, 99, disabled=False)
    assert missing is None


@pytest.mark.asyncio
async def test_disable_reminders_tool_toggles_flag(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=10,
        disable_reminders=False,
    )
    fake_session = _FakeSession(customer=customer)
    monkeypatch.setattr(
        "bot.llm.tools.AsyncSessionLocal",
        lambda: _FakeSessionManager(fake_session),
    )
    async def fake_load(session, customer_id):
        return customer if customer_id == 10 else None

    monkeypatch.setattr("bot.llm.tools._load_customer", fake_load)

    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    tool = tool_by_name(ctx, "disable_reminders", mode=DIALOG_MODE_BOOKING)

    off_msg = await tool(disabled=True)
    assert customer.disable_reminders is True
    assert "отключены" in off_msg.lower()
    assert fake_session.committed is True

    fake_session.committed = False
    on_msg = await tool(disabled=False)
    assert customer.disable_reminders is False
    assert "включены" in on_msg.lower()
    assert fake_session.committed is True


def test_disable_reminders_in_manage_and_booking_modes():
    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    manage_names = [f.__name__ for f in make_tools(ctx, mode=DIALOG_MODE_MANAGE)]
    booking_names = [f.__name__ for f in make_tools(ctx, mode=DIALOG_MODE_BOOKING)]
    assert "disable_reminders" in manage_names
    assert "disable_reminders" in booking_names

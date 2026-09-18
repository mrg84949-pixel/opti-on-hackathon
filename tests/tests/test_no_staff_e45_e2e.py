"""Wave 2 E.3: CRM no-staff gate (T45) — FSM staff step, handoff."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.llm.llm_engine as llm_engine
from bot.llm.booking_fsm import BOOKING_STEP_KEY, try_handle_booking_fsm
from bot.llm.client_messages import render_handoff_client_text
from test_booking_fsm import _patch_fsm_common
from test_llm_engine_extra import _FakeSession, _FakeSessionManager, _org

_NO_STAFF_REPLY = (
    "Специалисты пока не настроены. Напишите «человек», чтобы связаться с администратором."
)


def _staff_step_customer() -> SimpleNamespace:
    return SimpleNamespace(
        id=2,
        phone="tg:9",
        name="Anna",
        dialog_context={BOOKING_STEP_KEY: "staff"},
    )


def _patch_empty_staff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bot.llm.booking_fsm._load_active_staff",
        AsyncMock(return_value=[]),
    )


@pytest.mark.asyncio
async def test_e45_fsm_empty_staff_at_staff_step(monkeypatch: pytest.MonkeyPatch):
    customer = _staff_step_customer()
    _patch_fsm_common(monkeypatch, customer)
    _patch_empty_staff(monkeypatch)

    result = await try_handle_booking_fsm(
        org_id=1,
        channel="telegram",
        user_id="9",
        user_text="1",
    )

    assert result is not None
    reply, status = result
    assert status == "booking_fsm"
    assert reply == _NO_STAFF_REPLY
    assert "не настроены" in reply.lower()
    assert "человек" in reply.lower()


@pytest.mark.asyncio
async def test_e45_human_handoff_after_no_staff_hint(monkeypatch: pytest.MonkeyPatch):
    customer = _staff_step_customer()
    _patch_fsm_common(monkeypatch, customer)
    _patch_empty_staff(monkeypatch)

    handoff_called = {"count": 0}

    async def fake_handoff(*, org_id: int, customer_id: int | None):
        handoff_called["count"] += 1
        assert org_id == 1
        assert customer_id == 2

    monkeypatch.setattr(
        "bot.llm.booking_fsm.handoff_service.request_human_handoff",
        fake_handoff,
    )

    result = await try_handle_booking_fsm(
        org_id=1,
        channel="telegram",
        user_id="9",
        user_text="человек",
    )

    assert result is not None
    reply, status = result
    assert status == "booking_fsm"
    assert reply == render_handoff_client_text()
    assert "сообщите клиенту" not in reply.lower()
    assert handoff_called["count"] == 1


@pytest.mark.asyncio
async def test_e45_llm_engine_fsm_path_no_crash(monkeypatch: pytest.MonkeyPatch):
    llm_engine._sessions.clear()
    customer = SimpleNamespace(
        id=77,
        muted_until=None,
        dialog_context={BOOKING_STEP_KEY: "staff"},
    )
    captured: dict[str, object] = {}

    async def fake_customer(*args, **kwargs):
        return customer

    async def fake_maybe_auto_mute(session, cust):
        return False

    monkeypatch.setattr(llm_engine, "get_or_create_customer_for_channel", fake_customer)
    monkeypatch.setattr(llm_engine.customer_service, "maybe_auto_mute", fake_maybe_auto_mute)
    fake_session_mgr = _FakeSessionManager(_FakeSession(get_results={(llm_engine.Organization, 1): _org()}))
    monkeypatch.setattr(llm_engine, "AsyncSessionLocal", lambda: fake_session_mgr)
    monkeypatch.setattr("bot.llm.booking_fsm.AsyncSessionLocal", lambda: fake_session_mgr)
    monkeypatch.setattr(
        llm_engine.booking_deterministic,
        "try_handle_booking_shortcut",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        llm_engine.client_change_intent,
        "try_handle_client_change_reply",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "bot.llm.booking_fsm._load_active_staff",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "bot.llm.booking_fsm.get_or_create_customer_for_channel",
        fake_customer,
    )
    monkeypatch.setattr(
        "bot.llm.booking_fsm.resolve_tool_mode",
        AsyncMock(return_value="booking"),
    )
    monkeypatch.setattr("bot.llm.booking_fsm.settings", SimpleNamespace(ai_booking_fsm=True))
    monkeypatch.setattr(
        "bot.llm.booking_fsm.load_services_catalog_for_org",
        AsyncMock(return_value="- Консультация: 5000 ₸"),
    )

    async def fake_persist(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)

    result = await llm_engine.get_ai_response(
        "1946206956",
        "1",
        {},
        channel="telegram",
        org_id=1,
    )

    assert result == _NO_STAFF_REPLY
    assert captured.get("status") == "booking_fsm"
    assert captured.get("reply") == _NO_STAFF_REPLY

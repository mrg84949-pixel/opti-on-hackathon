"""Wave 2 B.5: booking edge cases — double confirm, confirm without draft."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot.llm.llm_engine as llm_engine
from bot.db.models import Appointment
from bot.llm.booking_draft import PENDING_BOOKING_KEY
from bot.llm.booking_fsm import BOOKING_STEP_KEY
from bot.services import booking_deterministic as bd

from test_bot_scenarios import (
    _FakeSession,
    _FakeSessionManager,
    _customer,
    _org,
    _patch_crm_provider,
)

_DEMO_STAFF = [
    {"id": "doc-1", "name": "Специалист 1", "active": True},
    {"id": "doc-2", "name": "Специалист 2", "active": True},
]

_COMPLETE_DRAFT = {
    "customer_name": "Alice",
    "service": "Консультация",
    "date": "2027-06-15",
    "time": "10:00",
    "doctor_id": "doc-1",
    "doctor_name": "Специалист 1",
}

_T40_STEPS = [
    "хочу записаться",
    "Тест Edge",
    "Консультация",
    "2",
    "2027-06-15",
    "10",
    "да",
    "да",
]


class _StubProvider:
    provider_name = "stub"

    def credentials_configured(self) -> bool:
        return True

    def missing_credentials_message(self) -> str:
        return ""

    async def create_session(self, _setup):
        return object()

    async def send_turn(self, _handle, _user_text):
        return "Принято."


def _make_session_manager(customer: SimpleNamespace, org: SimpleNamespace | None = None):
    org = org or SimpleNamespace(id=1, timezone="UTC")

    class _Session:
        async def get(self, model, key):
            return org

        async def commit(self):
            return None

        async def refresh(self, obj):
            return None

    class _SessionManager:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return False

    return _SessionManager()


def _patch_fsm_edge_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    org,
    customer,
    fake_session: _FakeSession,
) -> None:
    monkeypatch.setenv("AI_BOOKING_FSM", "1")
    llm_engine._sessions.clear()
    fake_session.org = org
    fake_session.customer = customer

    monkeypatch.setattr(
        llm_engine,
        "get_llm_provider",
        lambda _org=None: _StubProvider(),
    )
    session_factory = lambda: _FakeSessionManager(fake_session)
    for target in (
        llm_engine,
        "bot.llm.tools",
        "bot.services.booking_service",
        "bot.llm.booking_fsm",
        "bot.services.booking_deterministic",
    ):
        if isinstance(target, str):
            monkeypatch.setattr(f"{target}.AsyncSessionLocal", session_factory)
        else:
            monkeypatch.setattr(target, "AsyncSessionLocal", session_factory)

    async def fake_intent(**_kwargs):
        return None

    async def fake_get_or_create(_session, *, org_id, channel_phone, name=None):
        _ = org_id, channel_phone, name
        return customer

    async def fake_maybe_auto_mute(_session, _customer):
        return False

    async def fake_catalog(_session, _org_id):
        return "- Консультация: 5 000 ₽"

    async def fake_persist(**_kwargs):
        return None

    async def fake_notify_new(**_kwargs):
        return 1

    async def fake_price_minor(_session, _org_id, _service):
        return 500_000

    async def fake_notify_cancel(*_args, **_kwargs):
        return None

    async def fake_notify_reschedule(*_args, **_kwargs):
        return None

    async def fake_send_message(*_args, **_kwargs):
        return True

    staff_mock = AsyncMock(return_value=_DEMO_STAFF)

    async def _staff_count(_session, _org_id):
        return len(_DEMO_STAFF)

    monkeypatch.setattr(llm_engine, "get_or_create_customer_for_channel", fake_get_or_create)
    monkeypatch.setattr(
        llm_engine.client_change_intent,
        "try_handle_client_change_reply",
        fake_intent,
    )
    monkeypatch.setattr("bot.llm.booking_fsm.get_or_create_customer_for_channel", fake_get_or_create)
    monkeypatch.setattr("bot.services.booking_deterministic.get_or_create_customer_for_channel", fake_get_or_create)
    monkeypatch.setattr("bot.llm.booking_fsm.resolve_tool_mode", AsyncMock(return_value="booking"))
    monkeypatch.setattr("bot.services.booking_deterministic.resolve_tool_mode", AsyncMock(return_value="booking"))
    monkeypatch.setattr(llm_engine.customer_service, "maybe_auto_mute", fake_maybe_auto_mute)
    monkeypatch.setattr(llm_engine, "load_services_catalog_for_org", fake_catalog)
    monkeypatch.setattr("bot.llm.booking_fsm.load_services_catalog_for_org", fake_catalog)
    monkeypatch.setattr("bot.llm.tools.load_services_catalog_for_org", fake_catalog)
    monkeypatch.setattr("bot.services.booking_deterministic.load_services_catalog_for_org", fake_catalog)
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)
    monkeypatch.setattr("bot.llm.booking_fsm._load_active_staff", staff_mock)
    monkeypatch.setattr("bot.services.booking_deterministic._load_active_staff_count", _staff_count)
    monkeypatch.setattr("bot.llm.tools.notify_admins_about_new_appointment", fake_notify_new)
    monkeypatch.setattr("bot.llm.tools.resolve_service_price_minor", fake_price_minor)
    monkeypatch.setattr("bot.llm.tools._notify_client_cancel", fake_notify_cancel)
    monkeypatch.setattr("bot.llm.tools._notify_client_reschedule", fake_notify_reschedule)
    monkeypatch.setattr(
        "bot.llm.tools.notification_service.send_customer_message",
        fake_send_message,
    )
    _patch_crm_provider(monkeypatch)


@pytest.mark.asyncio
async def test_double_confirm_fsm_creates_single_appointment(monkeypatch: pytest.MonkeyPatch):
    customer = _customer()
    fake_session = _FakeSession(org=_org(), customer=customer)
    _patch_fsm_edge_env(
        monkeypatch,
        org=fake_session.org,
        customer=customer,
        fake_session=fake_session,
    )

    for text in _T40_STEPS:
        await llm_engine.get_ai_response(
            "user-1",
            text,
            channel="web",
            org_id=fake_session.org.id,
        )

    appts = [obj for obj in fake_session.added if isinstance(obj, Appointment)]
    assert len(appts) == 1
    assert PENDING_BOOKING_KEY not in customer.dialog_context
    assert customer.dialog_context.get(BOOKING_STEP_KEY) is None


@pytest.mark.asyncio
async def test_confirm_draft_returns_none_without_pending(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={},
    )
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(bd, "resolve_tool_mode", AsyncMock(return_value="booking"))
    monkeypatch.setattr(bd, "load_services_catalog_for_org", AsyncMock(return_value="catalog"))
    monkeypatch.setattr(bd, "_load_active_staff_count", AsyncMock(return_value=1))

    assert (
        await bd.try_handle_confirm_draft(
            org_id=1, channel="telegram", user_id="9", user_text="да"
        )
        is None
    )

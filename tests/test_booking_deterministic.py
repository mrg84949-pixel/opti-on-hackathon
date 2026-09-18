from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.llm.booking_draft import PENDING_BOOKING_KEY
from bot.llm.staff_choice import AWAITING_STAFF_CHOICE_KEY
from bot.llm.tools import DIALOG_MODE_MANAGE
from bot.services import booking_deterministic as bd
from bot.services import booking_service

_DEMO_STAFF = [
    {"id": "doc-1", "name": "Специалист 1"},
    {"id": "doc-2", "name": "Специалист 2"},
]

_COMPLETE_DRAFT = {
    "customer_name": "Alice",
    "service": "Консультация",
    "date": "2027-06-15",
    "time": "10:00",
    "doctor_id": "doc-1",
    "doctor_name": "Dr",
}


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


@pytest.mark.asyncio
async def test_try_handle_skips_admin_sandbox():
    assert await bd.try_handle_staff_choice(
        org_id=1, channel="telegram", user_id="admin-sandbox-1", user_text="2"
    ) is None


@pytest.mark.asyncio
async def test_try_handle_skips_without_awaiting_flag(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=2, phone="tg:9", dialog_context={})
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    assert await bd.try_handle_staff_choice(
        org_id=1, channel="telegram", user_id="9", user_text="2"
    ) is None


@pytest.mark.asyncio
async def test_try_handle_skips_unrecognized_choice(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={AWAITING_STAFF_CHOICE_KEY: _DEMO_STAFF},
    )
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    assert await bd.try_handle_staff_choice(
        org_id=1, channel="telegram", user_id="9", user_text="привет"
    ) is None


@pytest.mark.asyncio
async def test_try_handle_staff_by_index(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={AWAITING_STAFF_CHOICE_KEY: _DEMO_STAFF},
    )
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    reply = await bd.try_handle_staff_choice(
        org_id=1, channel="telegram", user_id="9", user_text="2"
    )
    assert reply is not None
    assert "Специалист 2" in reply
    assert "doc-2" not in reply
    assert "На какой день" in reply


@pytest.mark.asyncio
async def test_try_handle_clears_awaiting_and_sets_draft(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={AWAITING_STAFF_CHOICE_KEY: _DEMO_STAFF},
    )
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    await bd.try_handle_staff_choice(
        org_id=1, channel="telegram", user_id="9", user_text="Специалист 1"
    )
    ctx = customer.dialog_context
    assert AWAITING_STAFF_CHOICE_KEY not in ctx
    draft = ctx[PENDING_BOOKING_KEY]
    assert draft["doctor_id"] == "doc-1"
    assert draft["doctor_name"] == "Специалист 1"


@pytest.mark.asyncio
async def test_shortcut_router_staff_takes_priority(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={AWAITING_STAFF_CHOICE_KEY: _DEMO_STAFF},
    )
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    result = await bd.try_handle_booking_shortcut(
        org_id=1, channel="telegram", user_id="9", user_text="2"
    )
    assert result is not None
    reply, status = result
    assert status == "staff_choice"
    assert "Специалист 2" in reply


@pytest.mark.asyncio
async def test_confirm_skips_incomplete_draft(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={PENDING_BOOKING_KEY: {"customer_name": "Alice"}},
    )
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(bd, "resolve_tool_mode", AsyncMock(return_value="booking"))
    assert await bd.try_handle_confirm_draft(
        org_id=1, channel="telegram", user_id="9", user_text="да"
    ) is None


@pytest.mark.asyncio
async def test_confirm_skips_manage_mode(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={PENDING_BOOKING_KEY: dict(_COMPLETE_DRAFT)},
    )
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(bd, "resolve_tool_mode", AsyncMock(return_value=DIALOG_MODE_MANAGE))
    assert await bd.try_handle_confirm_draft(
        org_id=1, channel="telegram", user_id="9", user_text="да"
    ) is None


@pytest.mark.asyncio
async def test_confirm_yes_calls_booking_service(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={PENDING_BOOKING_KEY: dict(_COMPLETE_DRAFT)},
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

    async def fake_confirm(*, org_id: int, customer_id: int):
        assert org_id == 1
        assert customer_id == 2
        return booking_service.ConfirmBookingResult(
            appointment_id=42,
            when_label="15.06.2027 10:00",
            timezone_name="UTC",
            service_name="Консультация",
            auto_confirmed=False,
        )

    monkeypatch.setattr(bd.booking_service, "confirm_pending_booking", fake_confirm)

    reply = await bd.try_handle_confirm_draft(
        org_id=1, channel="telegram", user_id="9", user_text="да"
    )
    assert reply is not None
    assert "оформлена" in reply.lower()


@pytest.mark.asyncio
async def test_confirm_yes_returns_client_safe_text(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={PENDING_BOOKING_KEY: dict(_COMPLETE_DRAFT)},
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

    async def fake_confirm(*, org_id: int, customer_id: int):
        return booking_service.ConfirmBookingResult(
            appointment_id=42,
            when_label="15.06.2027 10:00",
            timezone_name="UTC",
            service_name="Консультация",
            auto_confirmed=False,
        )

    monkeypatch.setattr(bd.booking_service, "confirm_pending_booking", fake_confirm)

    reply = await bd.try_handle_confirm_draft(
        org_id=1, channel="telegram", user_id="9", user_text="да"
    )
    assert reply is not None
    assert "Сообщите клиенту" not in reply
    assert "cancel_appointment" not in reply
    assert "Контекст сжат" not in reply
    assert "статус «новая»" not in reply


@pytest.mark.asyncio
async def test_confirm_no_clears_draft(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={PENDING_BOOKING_KEY: dict(_COMPLETE_DRAFT)},
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

    async def fake_cancel():
        customer.dialog_context = {}
        return "Черновик записи отменён."

    monkeypatch.setattr(bd, "tool_by_name", lambda ctx, name: fake_cancel)

    reply = await bd.try_handle_confirm_draft(
        org_id=1, channel="telegram", user_id="9", user_text="нет"
    )
    assert reply is not None
    assert "Что изменить" in reply
    assert PENDING_BOOKING_KEY not in customer.dialog_context


@pytest.mark.asyncio
async def test_services_catalog_regex(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=2, phone="tg:9", dialog_context={})
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(bd, "resolve_tool_mode", AsyncMock(return_value="booking"))
    monkeypatch.setattr(
        bd,
        "load_services_catalog_for_org",
        AsyncMock(return_value="- Консультация: 5000 ₸"),
    )
    reply = await bd.try_handle_services_catalog(
        org_id=1, channel="telegram", user_id="9", user_text="какие услуги"
    )
    assert reply == "- Консультация: 5000 ₸"


@pytest.mark.asyncio
async def test_services_catalog_skips_manage(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=2, phone="tg:9", dialog_context={})
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(bd, "resolve_tool_mode", AsyncMock(return_value=DIALOG_MODE_MANAGE))
    assert await bd.try_handle_services_catalog(
        org_id=1, channel="telegram", user_id="9", user_text="прайс"
    ) is None


@pytest.mark.asyncio
async def test_shortcuts_disabled_by_flag(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={AWAITING_STAFF_CHOICE_KEY: _DEMO_STAFF},
    )
    monkeypatch.setattr(bd, "AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        bd,
        "get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr(bd.settings, "ai_booking_shortcuts", False)
    monkeypatch.setattr(
        bd,
        "load_services_catalog_for_org",
        AsyncMock(return_value="catalog"),
    )

    staff_result = await bd.try_handle_booking_shortcut(
        org_id=1, channel="telegram", user_id="9", user_text="2"
    )
    assert staff_result is not None
    assert staff_result[1] == "staff_choice"

    customer.dialog_context = {}
    assert await bd.try_handle_booking_shortcut(
        org_id=1, channel="telegram", user_id="9", user_text="прайс"
    ) is None

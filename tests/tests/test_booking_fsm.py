from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.llm.booking_draft import PENDING_BOOKING_KEY
from bot.llm.booking_fsm import (
    BOOKING_STEP_KEY,
    match_service_from_catalog,
    matches_booking_entry_intent,
    try_handle_booking_fsm,
)
from bot.llm.staff_choice import AWAITING_STAFF_CHOICE_KEY
from bot.llm.tools import DIALOG_MODE_MANAGE
from bot.services import booking_deterministic as bd
from bot.services import booking_service

_CATALOG = "- Консультация: 5000 ₸\n- Чистка: 3000 ₸"
_DEMO_STAFF = [
    {"id": "doc-1", "name": "Специалист 1"},
    {"id": "doc-2", "name": "Специалист 2"},
]


def _make_session_manager(customer: SimpleNamespace, org: SimpleNamespace | None = None):
    org = org or SimpleNamespace(id=1, timezone="UTC")

    class _Session:
        async def get(self, model, key):
            return org

        async def commit(self):
            return None

        async def flush(self):
            return None

        async def refresh(self, obj):
            return None

    class _SessionManager:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, *args):
            return False

    return _SessionManager()


def test_matches_booking_entry_intent():
    assert matches_booking_entry_intent("хочу записаться")
    assert matches_booking_entry_intent("Запишите меня")
    assert not matches_booking_entry_intent("привет")


def test_match_service_from_catalog():
    assert match_service_from_catalog("консультация", _CATALOG) == "Консультация"
    assert match_service_from_catalog("чистка", _CATALOG) == "Чистка"
    assert match_service_from_catalog("услуга", _CATALOG) is None


def _patch_fsm_common(monkeypatch: pytest.MonkeyPatch, customer: SimpleNamespace) -> None:
    monkeypatch.setattr("bot.llm.booking_fsm.AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        "bot.llm.booking_fsm.get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr("bot.llm.booking_fsm.resolve_tool_mode", AsyncMock(return_value="booking"))
    monkeypatch.setattr("bot.llm.booking_fsm.settings", SimpleNamespace(ai_booking_fsm=True))
    monkeypatch.setattr(
        "bot.llm.booking_fsm.load_services_catalog_for_org",
        AsyncMock(return_value=_CATALOG),
    )


@pytest.mark.asyncio
async def test_entry_intent_starts_fsm(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=2, phone="tg:9", name=None, dialog_context={})
    _patch_fsm_common(monkeypatch, customer)

    result = await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="9", user_text="хочу записаться"
    )
    assert result is not None
    reply, status = result
    assert status == "booking_fsm"
    assert "Как вас зовут" in reply
    assert customer.dialog_context[BOOKING_STEP_KEY] == "name"


@pytest.mark.asyncio
async def test_name_step_saves_and_shows_catalog(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        name=None,
        dialog_context={BOOKING_STEP_KEY: "name"},
    )
    _patch_fsm_common(monkeypatch, customer)

    async def fake_set_name(name):
        customer.name = name
        return f"Имя сохранено: {name}"

    monkeypatch.setattr(
        "bot.llm.booking_fsm.tool_by_name",
        lambda ctx, name: fake_set_name if name == "set_customer_name" else None,
    )

    result = await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="9", user_text="Анна"
    )
    assert result is not None
    reply, _ = result
    assert "Анна" in reply
    assert "Консультация" in reply
    assert customer.dialog_context[BOOKING_STEP_KEY] == "service"


@pytest.mark.asyncio
async def test_service_step_match_and_no_match(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        name="Анна",
        dialog_context={BOOKING_STEP_KEY: "service", PENDING_BOOKING_KEY: {"customer_name": "Анна"}},
    )
    _patch_fsm_common(monkeypatch, customer)
    monkeypatch.setattr(
        "bot.llm.booking_fsm._load_active_staff",
        AsyncMock(return_value=[{"id": "doc-1", "name": "Dr", "active": True}]),
    )

    ok = await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="9", user_text="консультация"
    )
    assert ok is not None
    assert "Консультация" in ok[0]
    assert customer.dialog_context[BOOKING_STEP_KEY] == "date"

    customer.dialog_context = {BOOKING_STEP_KEY: "service"}
    bad = await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="9", user_text="неизвестная услуга"
    )
    assert bad is not None
    assert "Не удалось определить услугу" in bad[0]


@pytest.mark.asyncio
async def test_date_step_shows_slots(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        name="Анна",
        dialog_context={
            BOOKING_STEP_KEY: "date",
            PENDING_BOOKING_KEY: {
                "customer_name": "Анна",
                "service": "Консультация",
                "doctor_id": "doc-1",
            },
        },
    )
    _patch_fsm_common(monkeypatch, customer)

    async def fake_slots(doctor_id, date_iso):
        return "Свободные окна на 2027-06-16: 10:00, 11:00. [Инструкция для LLM]"

    monkeypatch.setattr(
        "bot.llm.booking_fsm.tool_by_name",
        lambda ctx, name: fake_slots if name == "get_available_slots" else None,
    )

    result = await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="9", user_text="завтра"
    )
    assert result is not None
    reply, _ = result
    assert "10:00" in reply
    assert "[Инструкция" not in reply
    assert customer.dialog_context[BOOKING_STEP_KEY] == "time"


@pytest.mark.asyncio
async def test_time_step_shows_card_without_instruction(monkeypatch: pytest.MonkeyPatch):
    draft = {
        "customer_name": "Анна",
        "service": "Консультация",
        "date": "2027-06-16",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        name="Анна",
        dialog_context={BOOKING_STEP_KEY: "time", PENDING_BOOKING_KEY: dict(draft)},
    )
    _patch_fsm_common(monkeypatch, customer)

    async def fake_card(*args, **kwargs):
        customer.dialog_context = {
            BOOKING_STEP_KEY: "time",
            PENDING_BOOKING_KEY: {**draft, "time": "15:00"},
        }
        return "Карточка\n📋 Проверьте запись: [Инструкция для LLM]"

    monkeypatch.setattr(
        "bot.llm.booking_fsm.tool_by_name",
        lambda ctx, name: fake_card if name == "show_appointment_card" else None,
    )

    result = await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="9", user_text="в 15"
    )
    assert result is not None
    reply, _ = result
    assert "[Инструкция" not in reply
    assert "15:00" in reply or "Анна" in reply
    assert customer.dialog_context[BOOKING_STEP_KEY] == "confirm"


@pytest.mark.asyncio
async def test_time_step_rejects_invalid_slot_without_crash(monkeypatch: pytest.MonkeyPatch):
    draft = {
        "customer_name": "Анна",
        "service": "Консультация",
        "date": "2027-06-16",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        name="Анна",
        dialog_context={BOOKING_STEP_KEY: "time", PENDING_BOOKING_KEY: dict(draft)},
    )
    _patch_fsm_common(monkeypatch, customer)

    async def fake_card(*_args, **_kwargs):
        return "Это время недоступно. Выберите другое из списка."

    monkeypatch.setattr(
        "bot.llm.booking_fsm.tool_by_name",
        lambda ctx, name: fake_card if name == "show_appointment_card" else None,
    )

    result = await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="9", user_text="03:00"
    )
    assert result is not None
    reply, _ = result
    assert "недоступно" in reply.lower() or "окон" in reply.lower()
    assert customer.dialog_context[BOOKING_STEP_KEY] == "time"


@pytest.mark.asyncio
async def test_fsm_skips_flag_off_sandbox_manage(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(id=2, phone="tg:9", dialog_context={})
    monkeypatch.setattr("bot.llm.booking_fsm.AsyncSessionLocal", lambda: _make_session_manager(customer))
    monkeypatch.setattr(
        "bot.llm.booking_fsm.get_or_create_customer_for_channel",
        AsyncMock(return_value=customer),
    )
    monkeypatch.setattr("bot.llm.booking_fsm.settings", SimpleNamespace(ai_booking_fsm=False))
    monkeypatch.setattr(
        "bot.llm.booking_fsm.load_services_catalog_for_org",
        AsyncMock(return_value=_CATALOG),
    )
    assert await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="9", user_text="хочу записаться"
    ) is None

    monkeypatch.setattr("bot.llm.booking_fsm.settings", SimpleNamespace(ai_booking_fsm=True))
    monkeypatch.setattr(
        "bot.llm.booking_fsm.load_services_catalog_for_org",
        AsyncMock(return_value=_CATALOG),
    )
    assert await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="admin-sandbox-1", user_text="хочу записаться"
    ) is None

    monkeypatch.setattr("bot.llm.booking_fsm.resolve_tool_mode", AsyncMock(return_value=DIALOG_MODE_MANAGE))
    assert await try_handle_booking_fsm(
        org_id=1, channel="telegram", user_id="9", user_text="хочу записаться"
    ) is None


@pytest.mark.asyncio
async def test_staff_shortcut_advances_fsm_step(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={
            BOOKING_STEP_KEY: "staff",
            AWAITING_STAFF_CHOICE_KEY: _DEMO_STAFF,
        },
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
    assert customer.dialog_context[BOOKING_STEP_KEY] == "date"


@pytest.mark.asyncio
async def test_confirm_clears_booking_step(monkeypatch: pytest.MonkeyPatch):
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={PENDING_BOOKING_KEY: dict(draft), BOOKING_STEP_KEY: "confirm"},
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
            appointment_id=1,
            when_label="15.06.2027 10:00",
            timezone_name="UTC",
            service_name="Консультация",
            auto_confirmed=False,
        )

    monkeypatch.setattr(bd.booking_service, "confirm_pending_booking", fake_confirm)

    await bd.try_handle_confirm_draft(
        org_id=1, channel="telegram", user_id="9", user_text="да"
    )
    assert BOOKING_STEP_KEY not in customer.dialog_context


@pytest.mark.asyncio
async def test_fsm_human_request_client_safe(monkeypatch: pytest.MonkeyPatch):
    customer = SimpleNamespace(
        id=2,
        phone="tg:9",
        dialog_context={BOOKING_STEP_KEY: "name"},
    )
    _patch_fsm_common(monkeypatch, customer)

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
        org_id=1, channel="telegram", user_id="9", user_text="оператор"
    )
    assert result is not None
    reply, status = result
    assert status == "booking_fsm"
    assert "сообщите клиенту" not in reply.lower()
    assert "администратору" in reply.lower()
    assert handoff_called["count"] == 1

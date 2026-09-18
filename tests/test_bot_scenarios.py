from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import bot.llm.llm_engine as llm_engine
from bot.crm.base import Slot
from bot.llm.prompts import EMPTY_SERVICES_CATALOG
from bot.db.models import AppointmentStatus
from bot.llm.booking_draft import PAST_BOOKING_MESSAGE, PENDING_BOOKING_KEY
from bot.llm.providers.scripted_scenario import ScriptedScenarioProvider
from bot.llm.scenarios import (
    BOOKING_HAPPY_PATH,
    BOOKING_PAST_REJECTED,
    BOOKING_SLOT_REJECTED,
    BOOKING_THEN_CANCEL,
    BOOKING_THEN_RESCHEDULE,
    BOOKING_ZAVTRA,
    CONFIRM_WITHOUT_DRAFT,
    GET_SERVICES,
    BotScenario,
)
from bot.llm.tools import DIALOG_MODE_KEY, DIALOG_MODE_MANAGE, LAST_APPOINTMENT_ID_KEY


def _crm_slots_provider(slot_map: dict[str, list[tuple[int, int]]] | None = None):
    default_map = {
        "2027-06-01": [(10, 0)],
        "2027-06-02": [(15, 0)],
        "2027-06-15": [(10, 0), (11, 0), (12, 0)],
        "2027-06-16": [(15, 0)],
    }
    if slot_map:
        default_map.update(slot_map)

    class _Provider:
        async def get_available_slots(self, doctor_id: str, date_iso: str, *, tz_name: str | None = None):
            _ = doctor_id, tz_name
            slots: list[Slot] = []
            for hour, minute in default_map.get(date_iso, []):
                y, m, d = int(date_iso[:4]), int(date_iso[5:7]), int(date_iso[8:10])
                start = datetime(y, m, d, hour, minute)
                slots.append(Slot(start=start, end=start + timedelta(minutes=30)))
            return slots

        async def book_appointment(self, **kwargs):
            return SimpleNamespace(crm_appointment_id="crm-1")

    return _Provider()


def _patch_crm_provider(monkeypatch: pytest.MonkeyPatch, slot_map: dict[str, list[tuple[int, int]]] | None = None):
    provider = _crm_slots_provider(slot_map)
    monkeypatch.setattr("bot.services.appointment_service.get_crm_provider", lambda _org: provider)
    monkeypatch.setattr("bot.llm.tools.get_crm_provider", lambda _org: provider)


class _FakeExecuteResult:
    def __init__(self, *, scalar_one_or_none_value=None, one_or_none_value=None):
        self._scalar_one_or_none_value = scalar_one_or_none_value
        self._one_or_none_value = one_or_none_value

    def scalar_one_or_none(self):
        return self._scalar_one_or_none_value

    def one_or_none(self):
        return self._one_or_none_value

    def scalars(self):
        return self

    def first(self):
        return None


class _FakeSession:
    def __init__(self, *, org=None, customer=None, conflict=None):
        self.org = org
        self.customer = customer
        self.conflict = conflict
        self.active_row = None
        self.added = []

    async def get(self, model, key):
        model_name = getattr(model, "__name__", "")
        if model_name == "Organization":
            return self.org
        if model_name == "Customer":
            return self.customer
        if model_name == "Appointment":
            for appt in self.added:
                if getattr(appt, "id", None) == key:
                    return appt
            return None
        return None

    async def execute(self, _stmt):
        return _FakeExecuteResult(
            scalar_one_or_none_value=self.conflict,
            one_or_none_value=self.active_row,
        )

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = 99
        self.added.append(obj)

    async def commit(self):
        return None

    async def flush(self):
        return None

    async def refresh(self, _obj):
        return None


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _org(**overrides):
    base = {
        "id": 1,
        "system_prompt": "",
        "timezone": "UTC",
        "billing_paid_until": None,
        "bot_enabled": True,
        "crm_provider": "none",
        "crm_base_url": None,
        "crm_api_token": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _customer(*, dialog_context=None, name="Alice"):
    return SimpleNamespace(
        id=10,
        org_id=1,
        name=name,
        phone="tg:999",
        muted_until=None,
        dialog_context=dialog_context or {},
    )


def _sync_active_appointment(fake_session: _FakeSession, customer) -> None:
    appt_id = customer.dialog_context.get(LAST_APPOINTMENT_ID_KEY)
    if appt_id is None:
        fake_session.active_row = None
        return
    appt = next(
        (item for item in fake_session.added if getattr(item, "id", None) == int(appt_id)),
        None,
    )
    if appt is None:
        fake_session.active_row = None
        return
    status = appt.status
    if status in (AppointmentStatus.NEW, AppointmentStatus.CONFIRMED):
        fake_session.active_row = (appt, customer)
    else:
        fake_session.active_row = None


def _patch_bot_scenario_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scenario: BotScenario,
    org,
    customer,
    fake_session: _FakeSession,
    services_catalog: str = "- Консультация: 5 000 ₽",
) -> None:
    llm_engine._sessions.clear()
    fake_session.org = org
    fake_session.customer = customer

    provider = ScriptedScenarioProvider(scenario)
    monkeypatch.setattr(
        llm_engine,
        "get_llm_provider",
        lambda _org=None: provider,
    )
    session_factory = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr(llm_engine, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", session_factory)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", session_factory)

    async def fake_get_or_create(_session, *, org_id, channel_phone, name=None):
        _ = org_id, channel_phone, name
        return customer

    async def fake_maybe_auto_mute(_session, _customer):
        return False

    async def fake_catalog(_session, _org_id):
        return services_catalog

    async def fake_intent(**_kwargs):
        return None

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

    monkeypatch.setattr(llm_engine, "get_or_create_customer_for_channel", fake_get_or_create)
    monkeypatch.setattr(llm_engine.customer_service, "maybe_auto_mute", fake_maybe_auto_mute)
    monkeypatch.setattr(llm_engine, "load_services_catalog_for_org", fake_catalog)
    monkeypatch.setattr("bot.llm.tools.load_services_catalog_for_org", fake_catalog)
    monkeypatch.setattr(
        llm_engine.client_change_intent,
        "try_handle_client_change_reply",
        fake_intent,
    )
    monkeypatch.setattr(
        llm_engine.booking_deterministic,
        "try_handle_booking_shortcut",
        fake_intent,
    )
    monkeypatch.setattr(
        llm_engine.booking_fsm,
        "try_handle_booking_fsm",
        fake_intent,
    )
    monkeypatch.setattr(llm_engine, "_persist_interaction_log", fake_persist)
    monkeypatch.setattr("bot.llm.tools.notify_admins_about_new_appointment", fake_notify_new)
    monkeypatch.setattr("bot.llm.tools.resolve_service_price_minor", fake_price_minor)
    monkeypatch.setattr("bot.llm.tools._notify_client_cancel", fake_notify_cancel)
    monkeypatch.setattr("bot.llm.tools._notify_client_reschedule", fake_notify_reschedule)
    monkeypatch.setattr(
        "bot.llm.tools.notification_service.send_customer_message",
        fake_send_message,
    )
    _patch_crm_provider(monkeypatch)


async def _run_scenario(
    monkeypatch: pytest.MonkeyPatch,
    scenario: BotScenario,
    *,
    org=None,
    customer=None,
    services_catalog: str = "- Консультация: 5 000 ₽",
) -> tuple[list[str], _FakeSession]:
    org = org or _org()
    customer = customer or _customer()
    fake_session = _FakeSession(org=org, customer=customer)
    _patch_bot_scenario_env(
        monkeypatch,
        scenario=scenario,
        org=org,
        customer=customer,
        fake_session=fake_session,
        services_catalog=services_catalog,
    )

    replies: list[str] = []
    for turn in scenario.turns:
        reply = await llm_engine.get_ai_response(
            "user-1",
            turn.user_text,
            channel="web",
            org_id=org.id,
        )
        replies.append(reply)
        _sync_active_appointment(fake_session, customer)
    return replies, fake_session


@pytest.mark.asyncio
async def test_booking_happy_path_multi_turn(monkeypatch: pytest.MonkeyPatch):
    customer = _customer()
    replies, fake_session = await _run_scenario(
        monkeypatch, BOOKING_HAPPY_PATH, customer=customer
    )

    assert len(replies) == 2
    assert "Проверьте запись" in replies[0]
    assert "оформлена" in replies[1].lower()
    assert "сообщите клиенту" not in replies[1].lower()
    assert customer.dialog_context.get(DIALOG_MODE_KEY) == DIALOG_MODE_MANAGE
    assert PENDING_BOOKING_KEY not in customer.dialog_context
    assert customer.dialog_context.get(LAST_APPOINTMENT_ID_KEY) == 99
    assert len(fake_session.added) == 1
    assert fake_session.added[0].service_name == "Консультация"


@pytest.mark.asyncio
async def test_confirm_without_draft(monkeypatch: pytest.MonkeyPatch):
    customer = _customer()
    fake_session = _FakeSession(org=_org(), customer=customer)
    _patch_bot_scenario_env(
        monkeypatch,
        scenario=CONFIRM_WITHOUT_DRAFT,
        org=fake_session.org,
        customer=customer,
        fake_session=fake_session,
    )

    reply = await llm_engine.get_ai_response("user-1", "Да", channel="web", org_id=1)

    assert "Нет черновика" in reply
    assert len(fake_session.added) == 0


@pytest.mark.asyncio
async def test_get_services_catalog_reply(monkeypatch: pytest.MonkeyPatch):
    catalog = "- Консультация: 5 000 ₽\n- Чистка: 3 000 ₽"
    replies, _fake_session = await _run_scenario(
        monkeypatch,
        GET_SERVICES,
        services_catalog=catalog,
    )

    assert len(replies) == 1
    assert "Консультация" in replies[0]
    assert "Чистка" in replies[0]


@pytest.mark.asyncio
async def test_get_services_empty_catalog_reply(monkeypatch: pytest.MonkeyPatch):
    replies, _fake_session = await _run_scenario(
        monkeypatch,
        GET_SERVICES,
        services_catalog=EMPTY_SERVICES_CATALOG.strip(),
    )

    assert len(replies) == 1
    reply = replies[0]
    assert "не настроен" in reply.lower()
    for forbidden in ("5 000", "15 000", "Базовая консультация", "Стандартная услуга категории A"):
        assert forbidden not in reply


@pytest.mark.asyncio
async def test_booking_then_cancel_multi_turn(monkeypatch: pytest.MonkeyPatch):
    customer = _customer()
    replies, fake_session = await _run_scenario(
        monkeypatch, BOOKING_THEN_CANCEL, customer=customer
    )

    assert len(replies) == 3
    assert "Проверьте запись" in replies[0]
    assert "оформлена" in replies[1].lower()
    assert "сообщите клиенту" not in replies[1].lower()
    assert "отменена" in replies[2]
    assert "№99" in replies[2]
    assert customer.dialog_context.get(DIALOG_MODE_KEY) != DIALOG_MODE_MANAGE
    assert LAST_APPOINTMENT_ID_KEY not in customer.dialog_context
    assert fake_session.added[0].status == AppointmentStatus.CANCELLED


@pytest.mark.asyncio
async def test_booking_then_reschedule_multi_turn(monkeypatch: pytest.MonkeyPatch):
    customer = _customer()
    replies, fake_session = await _run_scenario(
        monkeypatch, BOOKING_THEN_RESCHEDULE, customer=customer
    )

    assert len(replies) == 3
    assert "Проверьте запись" in replies[0]
    assert "оформлена" in replies[1].lower()
    assert "сообщите клиенту" not in replies[1].lower()
    assert "перенесена" in replies[2]
    assert customer.dialog_context.get(DIALOG_MODE_KEY) == DIALOG_MODE_MANAGE
    assert customer.dialog_context.get(LAST_APPOINTMENT_ID_KEY) == 99
    appt = fake_session.added[0]
    assert appt.status == AppointmentStatus.NEW
    assert appt.scheduled_at == datetime(2027, 6, 2, 15, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_booking_zavtra_multi_turn(monkeypatch: pytest.MonkeyPatch):
    def fake_normalize_date(raw, tz_name, *, now=None):
        if (raw or "").strip().lower() in {"завтра", "на завтра"}:
            return "2027-06-16"
        return None

    monkeypatch.setattr("bot.llm.tools.normalize_booking_date", fake_normalize_date)
    monkeypatch.setattr("bot.services.appointment_service.normalize_booking_date", fake_normalize_date)
    customer = _customer()
    replies, fake_session = await _run_scenario(
        monkeypatch, BOOKING_ZAVTRA, customer=customer
    )

    assert len(replies) == 1
    assert "Проверьте запись" in replies[0]
    assert "15:00" in replies[0]
    assert len(fake_session.added) == 0


@pytest.mark.asyncio
async def test_booking_past_rejected(monkeypatch: pytest.MonkeyPatch):
    customer = _customer()
    replies, fake_session = await _run_scenario(
        monkeypatch, BOOKING_PAST_REJECTED, customer=customer
    )

    assert len(replies) == 1
    assert PAST_BOOKING_MESSAGE.split(".")[0] in replies[0] or "прошл" in replies[0].lower()
    assert PENDING_BOOKING_KEY not in customer.dialog_context
    assert len(fake_session.added) == 0


@pytest.mark.asyncio
async def test_booking_slot_rejected(monkeypatch: pytest.MonkeyPatch):
    customer = _customer()
    replies, fake_session = await _run_scenario(
        monkeypatch, BOOKING_SLOT_REJECTED, customer=customer
    )

    assert len(replies) == 1
    assert "уже занято" in replies[0]
    assert PENDING_BOOKING_KEY not in customer.dialog_context
    assert len(fake_session.added) == 0

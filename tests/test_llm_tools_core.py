from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from bot.llm.booking_draft import PENDING_BOOKING_KEY, TIME_VAGUE_MESSAGE
from bot.llm.context import TurnContext
from bot.llm.staff_choice import AWAITING_STAFF_CHOICE_KEY
from bot.llm.context_summary import CONTEXT_SUMMARY_KEY
from bot.llm.tools import (
    DIALOG_MODE_BOOKING,
    DIALOG_MODE_KEY,
    DIALOG_MODE_MANAGE,
    LAST_APPOINTMENT_ID_KEY,
    _resolve_booking_date_time,
    make_tools,
    tool_by_name,
)


def _patch_crm_provider(monkeypatch: pytest.MonkeyPatch, provider: object) -> None:
    monkeypatch.setattr("bot.services.appointment_service.get_crm_provider", lambda _org: provider)
    monkeypatch.setattr("bot.llm.tools.get_crm_provider", lambda _org: provider)


def _demo_org(**overrides):
    base = {
        "id": 1,
        "timezone": "UTC",
        "crm_provider": "none",
        "crm_base_url": None,
        "crm_api_token": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _slots_provider(*, year=2027, month=6, day=15, hour=10, minute=0):
    class _Provider:
        async def get_available_slots(self, **kwargs):
            return [
                SimpleNamespace(
                    start=datetime(year, month, day, hour, minute),
                    end=datetime(year, month, day, hour, minute + 30),
                )
            ]

        async def book_appointment(self, **kwargs):
            return SimpleNamespace(crm_appointment_id="crm-1")

    return _Provider()


_FUTURE_BOOKING_DATE = "2027-06-15"


class _FakeExecuteResult:
    def __init__(self, scalar_one_or_none_value=None):
        self._scalar_one_or_none_value = scalar_one_or_none_value

    def scalar_one_or_none(self):
        return self._scalar_one_or_none_value

    def scalars(self):
        return self

    def first(self):
        return None


class _FakeSession:
    def __init__(self, *, org=None, customer=None, conflict=None):
        self.org = org
        self.customer = customer
        self.conflict = conflict
        self.added = []

    async def get(self, model, key):
        model_name = getattr(model, "__name__", "")
        if model_name == "Organization":
            return self.org
        if model_name == "Customer":
            return self.customer
        return None

    async def execute(self, _stmt):
        return _FakeExecuteResult(self.conflict)

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


def _customer(*, dialog_context=None, name="Alice"):
    return SimpleNamespace(
        id=10,
        name=name,
        phone="tg:123",
        dialog_context=dialog_context or {},
    )


class AsyncMockNoop:
    async def __call__(self, **kwargs):
        return 1


@pytest.mark.asyncio
async def test_save_appointment_deprecated_message():
    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    save_appointment = tool_by_name(ctx, "save_appointment")
    result = await save_appointment("2027-06-15", "10:00")
    assert "show_appointment_card" in result


@pytest.mark.asyncio
async def test_book_appointment_deprecated_message():
    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    book_appointment = tool_by_name(ctx, "book_appointment")
    result = await book_appointment("doc-1", "2027-06-15 10:00")
    assert "confirm_appointment_booking" in result


@pytest.mark.asyncio
async def test_show_appointment_card_persists_draft(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    customer = _customer()
    fake_session = _FakeSession(org=org, customer=customer)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    _patch_crm_provider(monkeypatch, _slots_provider())

    show_card = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "show_appointment_card")
    result = await show_card("Alice", "Консультация", "2027-06-15", "10:00", "doc-1")

    assert "Проверьте запись" in result
    assert customer.dialog_context[PENDING_BOOKING_KEY]["service"] == "Консультация"
    assert customer.name == "Alice"


@pytest.mark.asyncio
async def test_confirm_appointment_booking_creates_and_clears_draft(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = _customer(dialog_context={PENDING_BOOKING_KEY: draft})
    fake_session = _FakeSession(org=org, customer=customer)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    _patch_crm_provider(monkeypatch, _slots_provider())
    called = {"new": 0}

    async def fake_notify_new(**kwargs):
        called["new"] += 1
        return 1

    monkeypatch.setattr("bot.llm.tools.notify_admins_about_new_appointment", fake_notify_new)

    confirm = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "confirm_appointment_booking")
    result = await confirm()

    assert "Запись создана" in result
    assert called["new"] == 1
    assert PENDING_BOOKING_KEY not in customer.dialog_context
    assert customer.dialog_context[DIALOG_MODE_KEY] == DIALOG_MODE_MANAGE
    assert customer.dialog_context[LAST_APPOINTMENT_ID_KEY] == 99
    assert CONTEXT_SUMMARY_KEY in customer.dialog_context
    assert "Консультация" in customer.dialog_context[CONTEXT_SUMMARY_KEY]
    assert len(fake_session.added) == 1


@pytest.mark.asyncio
async def test_confirm_pending_booking_concurrent_calls_book_once(monkeypatch: pytest.MonkeyPatch):
    """Two near-simultaneous confirms for the same customer must not double-book.

    Regression test for the audit finding: without the per-customer lock in
    booking_service._confirm_locks, both calls could read the same pending
    draft before either cleared it and both call provider.book_appointment().
    """
    import asyncio

    from bot.services import booking_service

    org = _demo_org()
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = _customer(dialog_context={PENDING_BOOKING_KEY: draft})
    fake_session = _FakeSession(org=org, customer=customer)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    book_calls = {"count": 0}

    class _SlowProvider:
        async def get_available_slots(self, **kwargs):
            return [
                SimpleNamespace(
                    start=datetime(2027, 6, 15, 10, 0),
                    end=datetime(2027, 6, 15, 10, 30),
                )
            ]

        async def book_appointment(self, **kwargs):
            book_calls["count"] += 1
            # Widen the race window so a missing lock would let both callers
            # observe the draft before either clears it.
            await asyncio.sleep(0.05)
            return SimpleNamespace(crm_appointment_id="crm-1")

    _patch_crm_provider(monkeypatch, _SlowProvider())
    monkeypatch.setattr("bot.llm.tools.notify_admins_about_new_appointment", AsyncMockNoop())

    results = await asyncio.gather(
        booking_service.confirm_pending_booking(org_id=1, customer_id=10),
        booking_service.confirm_pending_booking(org_id=1, customer_id=10),
    )

    ok_results = [r for r in results if r.ok]
    error_results = [r for r in results if not r.ok]
    assert len(ok_results) == 1
    assert len(error_results) == 1
    assert "Нет черновика" in error_results[0].error
    assert book_calls["count"] == 1
    assert len(fake_session.added) == 1
    assert PENDING_BOOKING_KEY not in customer.dialog_context


@pytest.mark.asyncio
async def test_confirm_without_draft_returns_error(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    customer = _customer()
    fake_session = _FakeSession(org=org, customer=customer)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    confirm = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "confirm_appointment_booking")
    result = await confirm()
    assert "Нет черновика" in result


@pytest.mark.asyncio
async def test_book_appointment_returns_conflict_via_confirm(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = _customer(dialog_context={PENDING_BOOKING_KEY: draft})
    fake_session = _FakeSession(org=org, customer=customer, conflict=SimpleNamespace(id=1))
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    confirm = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "confirm_appointment_booking")
    result = await confirm()
    assert "уже занято" in result


@pytest.mark.asyncio
async def test_confirm_booking_ignores_conflict_in_other_org(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org(id=2)
    draft = {
        "customer_name": "Bob",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = _customer(dialog_context={PENDING_BOOKING_KEY: draft}, name="Bob")
    fake_session = _FakeSession(org=org, customer=customer, conflict=None)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    async def fake_notify_new(**kwargs):
        return 1

    monkeypatch.setattr("bot.llm.tools.notify_admins_about_new_appointment", fake_notify_new)

    class _Provider:
        async def get_available_slots(self, **kwargs):
            return [
                SimpleNamespace(
                    start=datetime(2027, 6, 15, 10, 0),
                    end=datetime(2027, 6, 15, 10, 30),
                )
            ]

        async def book_appointment(self, **kwargs):
            return SimpleNamespace(crm_appointment_id="crm-2")

    _patch_crm_provider(monkeypatch, _Provider())

    confirm = tool_by_name(TurnContext(org_id=2, customer_id=10, services_catalog=""), "confirm_appointment_booking")
    result = await confirm()
    assert "Запись создана" in result
    assert len(fake_session.added) == 1


@pytest.mark.asyncio
async def test_confirm_rechecks_crm_slot_before_book(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = _customer(dialog_context={PENDING_BOOKING_KEY: draft})
    fake_session = _FakeSession(org=org, customer=customer, conflict=None)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    class _Provider:
        async def get_available_slots(self, **kwargs):
            return [
                SimpleNamespace(
                    start=datetime(2027, 6, 15, 11, 0),
                    end=datetime(2027, 6, 15, 11, 30),
                )
            ]

        async def book_appointment(self, **kwargs):
            raise AssertionError("book_appointment should not run when slot missing")

    _patch_crm_provider(monkeypatch, _Provider())

    confirm = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "confirm_appointment_booking")
    result = await confirm()
    assert "уже занято" in result
    assert PENDING_BOOKING_KEY in customer.dialog_context
    assert len(fake_session.added) == 0


@pytest.mark.asyncio
async def test_confirm_crm_book_failure_maps_to_slot_taken(monkeypatch: pytest.MonkeyPatch):
    import httpx

    org = _demo_org()
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }
    customer = _customer(dialog_context={PENDING_BOOKING_KEY: draft})
    fake_session = _FakeSession(org=org, customer=customer, conflict=None)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    class _Provider:
        async def get_available_slots(self, **kwargs):
            return [
                SimpleNamespace(
                    start=datetime(2027, 6, 15, 10, 0),
                    end=datetime(2027, 6, 15, 10, 30),
                )
            ]

        async def book_appointment(self, **kwargs):
            request = httpx.Request("POST", "https://crm.example/book")
            response = httpx.Response(409, request=request)
            raise httpx.HTTPStatusError("conflict", request=request, response=response)

    _patch_crm_provider(monkeypatch, _Provider())

    confirm = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "confirm_appointment_booking")
    result = await confirm()
    assert "уже занято" in result
    assert PENDING_BOOKING_KEY in customer.dialog_context
    assert len(fake_session.added) == 0


@pytest.mark.asyncio
async def test_transfer_to_human_sets_flag_notifies_and_mutes(monkeypatch: pytest.MonkeyPatch):
    notify_called = {"count": 0}
    mute_called = {"count": 0}
    fake_session = _FakeSession()
    fake_session.committed = False

    async def _commit():
        fake_session.committed = True

    fake_session.commit = _commit

    async def _mute(session, customer_id, org_id):
        mute_called["count"] += 1
        assert customer_id == 10
        assert org_id == 1
        return True

    async def _notify(*, org_id: int, customer_id: int):
        notify_called["count"] += 1
        assert org_id == 1
        assert customer_id == 10
        return 1

    monkeypatch.setattr("bot.services.handoff_service.customer_service.mute_for_human_handoff", _mute)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.services.handoff_service.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.handoff_service.notify_admins_about_human_transfer", _notify)
    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    transfer_to_human = tool_by_name(ctx, "transfer_to_human")
    result = await transfer_to_human()
    assert "Запрос передан администратору" in result
    assert ctx.human_transfer_requested is True
    assert mute_called["count"] == 1
    assert fake_session.committed is True
    assert notify_called["count"] == 1


@pytest.mark.asyncio
async def test_show_appointment_card_rejects_past_date(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    customer = _customer()
    fake_session = _FakeSession(org=org, customer=customer)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    show_card = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "show_appointment_card")
    result = await show_card("Alice", "Консультация", "2024-01-15", "10:00")
    assert "прошедшую" in result.lower() or "прошед" in result.lower()


@pytest.mark.asyncio
async def test_get_available_slots_rejects_past_date(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()

    class _Provider:
        async def get_available_slots(self, **kwargs):
            return []

    fake_session = _FakeSession(org=org)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.llm.tools.get_crm_provider", lambda _org: _Provider())

    slots = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "get_available_slots")
    result = await slots("doc-1", "2020-01-01")
    assert "прошла" in result


@pytest.mark.asyncio
async def test_get_available_slots_accepts_zavtra(monkeypatch: pytest.MonkeyPatch):
    from datetime import date as date_cls

    org = _demo_org()

    class _Provider:
        async def get_available_slots(self, **kwargs):
            assert kwargs["date_iso"] >= date_cls.today().isoformat()
            return [SimpleNamespace(start=datetime(2027, 6, 15, 10, 0))]

    fake_session = _FakeSession(org=org)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.llm.tools.get_crm_provider", lambda _org: _Provider())

    slots = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "get_available_slots")
    result = await slots("doc-1", "завтра")
    assert "Свободные окна" in result


@pytest.mark.asyncio
async def test_list_crm_staff_returns_demo_items(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    customer = _customer()
    fake_session = _FakeSession(org=org, customer=customer)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    staff_tool = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "list_crm_staff")
    result = await staff_tool()

    assert "1. Специалист 1" in result
    assert "2. Специалист 2" in result
    assert "К кому записать" in result
    assert "[Инструкция" in result
    client_part = result.split("[id для tools")[0]
    assert "doc-1" not in client_part
    assert "doc-2" not in client_part
    assert AWAITING_STAFF_CHOICE_KEY in customer.dialog_context


@pytest.mark.asyncio
async def test_list_crm_staff_org_missing(monkeypatch: pytest.MonkeyPatch):
    fake_session = _FakeSession(org=None)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    staff_tool = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "list_crm_staff")
    result = await staff_tool()

    assert "организация не найдена" in result


@pytest.mark.asyncio
async def test_list_crm_staff_empty_with_hint(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(id=1, timezone="UTC")

    async def fake_list(_session, _org_id):
        return {
            "source": "crm",
            "items": [],
            "hint": "Выполните sync для загрузки специалистов из CRM.",
        }

    fake_session = _FakeSession(org=org)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.llm.tools.crm_staff_service.list_crm_staff", fake_list)

    staff_tool = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "list_crm_staff")
    result = await staff_tool()

    assert "не загружены" in result.lower() or "Инструкция" in result
    assert "sync" in result.lower()


def test_booking_tool_names_include_list_crm_staff():
    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    booking_names = [f.__name__ for f in make_tools(ctx, mode=DIALOG_MODE_BOOKING)]
    assert "list_crm_staff" in booking_names


@pytest.mark.asyncio
async def test_get_available_slots_auto_single_staff(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()

    async def fake_list(_session, _org_id):
        return {
            "source": "demo",
            "items": [
                {
                    "id": "doc-1",
                    "name": "Специалист 1",
                    "work_start": "08:00",
                    "work_end": "20:00",
                    "active": True,
                }
            ],
        }

    fake_session = _FakeSession(org=org)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.llm.tools.crm_staff_service.list_crm_staff", fake_list)
    _patch_crm_provider(monkeypatch, _slots_provider())

    slots = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "get_available_slots")
    result = await slots("", "2027-06-15")
    assert "Свободные окна" in result
    assert "Специалист 1" in result
    assert "doc-1" not in result


@pytest.mark.asyncio
async def test_get_available_slots_requires_choice_multi_staff(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    fake_session = _FakeSession(org=org)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    slots = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "get_available_slots")
    result = await slots("", "2027-06-15")
    assert "К кому записать" in result
    assert "1. Специалист 1" in result
    assert "list_crm_staff" not in result


@pytest.mark.asyncio
async def test_get_available_slots_by_index_multi_staff(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    fake_session = _FakeSession(org=org)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    _patch_crm_provider(monkeypatch, _slots_provider())

    slots = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "get_available_slots")
    result = await slots("2", "2027-06-15")
    assert "Свободные окна" in result
    assert "Специалист 2" in result


@pytest.mark.asyncio
async def test_get_available_slots_by_name_multi_staff(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    fake_session = _FakeSession(org=org)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    _patch_crm_provider(monkeypatch, _slots_provider())

    slots = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "get_available_slots")
    result = await slots("Специалист 2", "2027-06-15")
    assert "Свободные окна" in result
    assert "Специалист 2" in result


@pytest.mark.asyncio
async def test_show_appointment_card_rejects_invalid_slot(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    customer = _customer()
    fake_session = _FakeSession(org=org, customer=customer)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    _patch_crm_provider(monkeypatch, _slots_provider(hour=10))

    show_card = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "show_appointment_card")
    result = await show_card("Alice", "Консультация", "2027-06-15", "03:00", "doc-1")
    assert "уже занято" in result


@pytest.mark.asyncio
async def test_confirm_without_doctor_rejects_multi_staff(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": None,
        "doctor_name": None,
    }
    customer = _customer(dialog_context={PENDING_BOOKING_KEY: draft})
    fake_session = _FakeSession(org=org, customer=customer)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    confirm = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "confirm_appointment_booking")
    result = await confirm()
    assert "К кому записать" in result
    assert len(fake_session.added) == 0


def test_resolve_booking_date_time_vague_time():
    _, _, err = _resolve_booking_date_time("завтра", "вечером", "UTC")
    assert err == TIME_VAGUE_MESSAGE
    assert "get_available_slots" not in err
    assert "свободных окон" in err


@pytest.mark.asyncio
async def test_show_appointment_card_accepts_natural_time_in_15(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    customer = _customer()
    fake_session = _FakeSession(org=org, customer=customer)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)
    _patch_crm_provider(monkeypatch, _slots_provider(hour=15))

    show_card = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "show_appointment_card")
    result = await show_card("Alice", "Консультация", "2027-06-15", "в 15", "doc-1")
    assert "Проверьте запись" in result
    assert "15:00" in result
    assert customer.dialog_context[PENDING_BOOKING_KEY]["time"] == "15:00"


@pytest.mark.asyncio
async def test_get_available_slots_sunday_empty(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    fake_session = _FakeSession(org=org)
    mgr = lambda: _FakeSessionManager(fake_session)
    monkeypatch.setattr("bot.llm.tools.AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)

    slots = tool_by_name(TurnContext(org_id=1, customer_id=10, services_catalog=""), "get_available_slots")
    result = await slots("doc-1", "2027-06-06")
    assert "Свободных окон" in result

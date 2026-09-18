from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import bot.llm.tools as llm_tools
from bot.llm.context import TurnContext
from bot.llm.prompts import EMPTY_SERVICES_CATALOG


class _ScalarOneOrNoneResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def first(self):
        return None


class _FakeSession:
    def __init__(self, *, org=None, customer=None, conflict=None, existing_customer=None):
        self.org = org
        self.customer = customer
        self.conflict = conflict
        self.existing_customer = existing_customer
        self.added = []
        self.committed = False
        self.refreshed = False

    async def get(self, model, _key):
        model_name = getattr(model, "__name__", "")
        if model_name == "Organization":
            return self.org
        if model_name == "Customer":
            return self.customer
        return None

    async def execute(self, _stmt):
        if self.existing_customer is not None:
            return _ScalarOneOrNoneResult(self.existing_customer)
        return _ScalarOneOrNoneResult(self.conflict)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = len(self.added) + 1
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    async def refresh(self, _obj):
        self.refreshed = True


class _FakeSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_tool_sessions(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    mgr = lambda s=session: _FakeSessionManager(s)
    monkeypatch.setattr(llm_tools, "AsyncSessionLocal", mgr)
    monkeypatch.setattr("bot.services.booking_service.AsyncSessionLocal", mgr)


@pytest.mark.asyncio
async def test_set_customer_name_missing_customer(monkeypatch: pytest.MonkeyPatch):
    no_customer_session = _FakeSession(
        org=SimpleNamespace(
            id=1,
            timezone="UTC",
            crm_provider="none",
            crm_base_url=None,
            crm_api_token=None,
        )
    )
    _patch_tool_sessions(monkeypatch, no_customer_session)
    ctx = TurnContext(org_id=1, customer_id=None, services_catalog="")
    set_name = llm_tools.tool_by_name(ctx, "set_customer_name")
    assert "профиль клиента не найден" in await set_name("Alice")


@pytest.mark.asyncio
async def test_card_and_confirm_success(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(
        id=1,
        timezone="Bad/Timezone",
        crm_provider="none",
        crm_base_url=None,
        crm_api_token=None,
    )
    customer = SimpleNamespace(id=10, name="Alice", phone="wa:777", dialog_context={})
    card_session = _FakeSession(org=org, customer=customer)
    _patch_tool_sessions(monkeypatch, card_session)
    called = {"new": 0}

    class _Provider:
        async def get_available_slots(self, **kwargs):
            return [
                SimpleNamespace(
                    start=datetime(2027, 6, 15, 10, 0),
                    end=datetime(2027, 6, 15, 10, 30),
                )
            ]

        async def book_appointment(self, **kwargs):
            return SimpleNamespace(crm_appointment_id="crm-1")

    monkeypatch.setattr(llm_tools, "get_crm_provider", lambda _org: _Provider())
    monkeypatch.setattr("bot.services.appointment_service.get_crm_provider", lambda _org: _Provider())

    async def fake_notify_new(**kwargs):
        called["new"] += 1
        assert kwargs["timezone_name"] in {"Bad/Timezone", "UTC"}
        return 1

    monkeypatch.setattr(llm_tools, "notify_admins_about_new_appointment", fake_notify_new)
    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="")
    card_result = await llm_tools.tool_by_name(ctx, "show_appointment_card")(
        "Alice", "Услуга", "2027-06-15", "10:00", "doc-1"
    )
    assert "Проверьте запись" in card_result

    confirm_session = _FakeSession(org=org, customer=customer)
    _patch_tool_sessions(monkeypatch, confirm_session)
    confirm_result = await llm_tools.tool_by_name(ctx, "confirm_appointment_booking")()
    assert "Запись создана" in confirm_result
    assert called["new"] == 1

    customer2 = SimpleNamespace(
        id=10,
        name="Alice",
        phone="wa:777",
        dialog_context={
            "pending_booking": {
                "customer_name": "Alice",
                "service": "Услуга",
                "date": "2027-06-15",
                "time": "10:00",
                "doctor_id": "doc-1",
                "doctor_name": "Dr",
            }
        },
    )
    book_session = _FakeSession(
        org=SimpleNamespace(
            id=1,
            timezone="UTC",
            crm_provider="none",
            crm_base_url=None,
            crm_api_token=None,
        ),
        customer=customer2,
        conflict=None,
    )
    _patch_tool_sessions(monkeypatch, book_session)
    confirm_tool = llm_tools.tool_by_name(
        TurnContext(org_id=1, customer_id=10, services_catalog=""),
        "confirm_appointment_booking",
    )
    crm_result = await confirm_tool()
    assert "Запись создана" in crm_result


@pytest.mark.asyncio
async def test_get_available_slots_and_book_errors(monkeypatch: pytest.MonkeyPatch):
    no_org_session = _FakeSession(org=None)
    _patch_tool_sessions(monkeypatch, no_org_session)
    slots_tool = llm_tools.tool_by_name(
        TurnContext(org_id=1, customer_id=10, services_catalog=""),
        "get_available_slots",
    )
    assert "организация не найдена" in await slots_tool("doc-1", "2027-06-15")

    class _ProviderError:
        async def get_available_slots(self, **kwargs):
            raise RuntimeError("crm boom")

        async def book_appointment(self, **kwargs):
            raise RuntimeError("book boom")

    org = SimpleNamespace(
        id=1,
        timezone="UTC",
        crm_provider="none",
        crm_base_url=None,
        crm_api_token=None,
    )
    customer = SimpleNamespace(id=10, name="Alice", phone="wa:777")
    error_session = _FakeSession(org=org, customer=customer, conflict=None)
    _patch_tool_sessions(monkeypatch, error_session)
    monkeypatch.setattr(llm_tools, "get_crm_provider", lambda org_arg: _ProviderError())
    slots_tool = llm_tools.tool_by_name(
        TurnContext(org_id=1, customer_id=10, services_catalog=""),
        "get_available_slots",
    )
    assert "crm boom" in await slots_tool("doc-1", "2027-06-15")

    book_tool = llm_tools.tool_by_name(
        TurnContext(org_id=1, customer_id=10, services_catalog=""),
        "book_appointment",
    )
    assert "confirm_appointment_booking" in await book_tool("doc-1", "2027-06-15 10:00")


@pytest.mark.asyncio
async def test_get_available_slots_success_and_services_fallback(monkeypatch: pytest.MonkeyPatch):
    class _Slot:
        def __init__(self, hour):
            self.start = datetime(2027, 6, 15, hour, 0)

    class _Provider:
        async def get_available_slots(self, **kwargs):
            return [_Slot(9), _Slot(10)]

    org = SimpleNamespace(
        id=1,
        timezone="UTC",
        crm_provider="none",
        crm_base_url=None,
        crm_api_token=None,
    )
    session = _FakeSession(org=org)
    _patch_tool_sessions(monkeypatch, session)
    monkeypatch.setattr(llm_tools, "get_crm_provider", lambda org_arg: _Provider())

    catalog_holder = {"value": ""}

    async def _fake_catalog(_session, _org_id):
        return catalog_holder["value"]

    monkeypatch.setattr(llm_tools, "load_services_catalog_for_org", _fake_catalog)

    ctx = TurnContext(org_id=1, customer_id=10, services_catalog="stale-snapshot-should-be-ignored")
    slots_tool = llm_tools.tool_by_name(ctx, "get_available_slots")
    services_tool = llm_tools.tool_by_name(ctx, "get_services_info")
    slots_result = await slots_tool("doc-1", "2027-06-15")
    assert "09:00, 10:00" in slots_result
    assert await services_tool() == EMPTY_SERVICES_CATALOG.strip()

    # Catalog changes mid-conversation (same ctx/tool instance) must be reflected
    # on the next call — get_services_info must not serve a stale ctx snapshot.
    catalog_holder["value"] = "Custom catalog"
    assert await services_tool() == "Custom catalog"


@pytest.mark.asyncio
async def test_transfer_without_customer_load_catalog_and_customer_creation(monkeypatch: pytest.MonkeyPatch):
    notified = {"count": 0}

    async def fake_notify(**kwargs):
        notified["count"] += 1

    monkeypatch.setattr(
        "bot.services.handoff_service.notify_admins_about_human_transfer",
        fake_notify,
    )
    ctx = TurnContext(org_id=1, customer_id=None, services_catalog="")
    result = await llm_tools.tool_by_name(ctx, "transfer_to_human")()
    assert "администратору" in result
    assert notified["count"] == 0

    async def fake_load(_session, _org_id):
        return EMPTY_SERVICES_CATALOG

    monkeypatch.setattr(
        "bot.services.org_services_catalog.load_services_catalog_for_org",
        fake_load,
    )
    assert await llm_tools.load_services_catalog_for_org(_FakeSession(org=None), 1) == EMPTY_SERVICES_CATALOG

    existing = SimpleNamespace(id=55, phone="wa:777")
    existing_session = _FakeSession(existing_customer=existing)
    found = await llm_tools.get_or_create_customer_for_channel(existing_session, org_id=1, channel_phone="wa:777")
    assert found is existing

    new_session = _FakeSession(existing_customer=None)
    created = await llm_tools.get_or_create_customer_for_channel(
        new_session,
        org_id=1,
        channel_phone="wa:888",
        name="New",
    )
    assert created.phone == "wa:888"
    assert new_session.committed is True
    assert new_session.refreshed is True


@pytest.mark.asyncio
async def test_get_available_slots_rejects_past_date(monkeypatch: pytest.MonkeyPatch):
    org = SimpleNamespace(
        id=1,
        timezone="UTC",
        crm_provider="none",
        crm_base_url=None,
        crm_api_token=None,
    )

    class _Provider:
        async def get_available_slots(self, **kwargs):
            return []

    session = _FakeSession(org=org)
    _patch_tool_sessions(monkeypatch, session)
    monkeypatch.setattr(llm_tools, "get_crm_provider", lambda _org: _Provider())

    slots_tool = llm_tools.tool_by_name(
        TurnContext(org_id=1, customer_id=10, services_catalog=""),
        "get_available_slots",
    )
    result = await slots_tool("doc-1", "2020-06-01")
    assert "прошла" in result

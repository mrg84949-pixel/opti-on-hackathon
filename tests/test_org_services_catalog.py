from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.db.models import Organization, OrganizationService
from bot.llm.prompts import EMPTY_SERVICES_CATALOG
from bot.services import org_services_catalog


class _FakeSession:
    def __init__(self, *, org=None, services: list[OrganizationService] | None = None):
        self._org = org
        self._services = services or []

    async def get(self, model, key):
        if model is Organization:
            return self._org if self._org is not None and getattr(self._org, "id", None) == key else None
        return None

    async def execute(self, stmt):
        rows = list(self._services)
        compiled = str(stmt).lower()
        if "is_active" in compiled and "organization_services" in compiled:
            rows = [row for row in rows if getattr(row, "is_active", True)]
        return _FakeResult(rows)

    async def delete(self, row):
        if row in self._services:
            self._services.remove(row)

    async def flush(self):
        return None


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def _service(org_id: int, name: str, **kwargs) -> OrganizationService:
    return OrganizationService(
        id=kwargs.pop("id", 1),
        org_id=org_id,
        name=name,
        price_label=kwargs.pop("price_label", None),
        description=kwargs.pop("description", None),
        care_message=kwargs.pop("care_message", None),
        sort_order=kwargs.pop("sort_order", 0),
        is_active=kwargs.pop("is_active", True),
    )


def test_format_services_catalog_text_bullets():
    services = [
        _service(1, "Консультация", price_label="5 000", id=1),
        _service(1, "УЗИ", price_label="от 12 000", description="Без контраста", id=2),
        _service(1, "Скрытая", is_active=False, id=3),
    ]
    text = org_services_catalog.format_services_catalog_text(services)
    assert "- Консультация: 5 000" in text
    assert "- УЗИ: от 12 000" in text
    assert "Без контраста" in text
    assert "Скрытая" not in text


@pytest.mark.asyncio
async def test_load_services_catalog_fallback_when_empty():
    org = SimpleNamespace(id=1)
    session = _FakeSession(org=org, services=[])
    text = await org_services_catalog.load_services_catalog_for_org(session, 1)
    assert text == EMPTY_SERVICES_CATALOG


@pytest.mark.asyncio
async def test_load_services_catalog_fallback_when_org_missing():
    session = _FakeSession(org=None, services=[])
    text = await org_services_catalog.load_services_catalog_for_org(session, 99)
    assert text == EMPTY_SERVICES_CATALOG


@pytest.mark.asyncio
async def test_org_has_active_services_false_when_empty():
    org = SimpleNamespace(id=1)
    session = _FakeSession(org=org, services=[])
    assert await org_services_catalog.org_has_active_services(session, 1) is False


@pytest.mark.asyncio
async def test_org_has_active_services_true_when_active_row():
    org = SimpleNamespace(id=1)
    services = [_service(1, "Консультация", id=1)]
    session = _FakeSession(org=org, services=services)
    assert await org_services_catalog.org_has_active_services(session, 1) is True


@pytest.mark.asyncio
async def test_org_has_active_services_false_when_only_inactive():
    org = SimpleNamespace(id=1)
    services = [_service(1, "Скрытая", is_active=False, id=1)]
    session = _FakeSession(org=org, services=services)
    assert await org_services_catalog.org_has_active_services(session, 1) is False


@pytest.mark.asyncio
async def test_load_services_catalog_per_org_isolation():
    org_a = SimpleNamespace(id=1)
    services_a = [_service(1, "Org A услуга", price_label="1 000", id=10)]
    session_a = _FakeSession(org=org_a, services=services_a)
    text_a = await org_services_catalog.load_services_catalog_for_org(session_a, 1)

    org_b = SimpleNamespace(id=2)
    services_b = [_service(2, "Org B услуга", price_label="2 000", id=20)]
    session_b = _FakeSession(org=org_b, services=services_b)
    text_b = await org_services_catalog.load_services_catalog_for_org(session_b, 2)

    assert "Org A услуга: 1 000" in text_a
    assert "Org B" not in text_a
    assert "Org B услуга: 2 000" in text_b
    assert "Org A" not in text_b


@pytest.mark.asyncio
async def test_resolve_care_message_returns_text_for_matching_service():
    services = [_service(1, "Консультация", care_message="Полоскайтесь тёплой водой.", id=1)]
    session = _FakeSession(services=services)
    text = await org_services_catalog.resolve_care_message(session, 1, "Консультация")
    assert text == "Полоскайтесь тёплой водой."


@pytest.mark.asyncio
async def test_resolve_care_message_returns_none_when_empty():
    session = _FakeSession(services=[])
    text = await org_services_catalog.resolve_care_message(session, 1, "Консультация")
    assert text is None


@pytest.mark.asyncio
async def test_resolve_care_message_returns_none_when_blank():
    services = [_service(1, "Консультация", care_message="   ", id=1)]
    session = _FakeSession(services=services)
    text = await org_services_catalog.resolve_care_message(session, 1, "Консультация")
    assert text is None

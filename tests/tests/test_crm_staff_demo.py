from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.db.models import OrganizationCrmStaffCache
from bot.services import crm_staff_service


class _FakeSession:
    def __init__(self, get_results: dict):
        self._get_results = get_results
        self.committed = False
        self.added: list[object] = []

    async def get(self, model, key):
        return self._get_results.get((model, key))

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self):
        self.committed = True


def _org(**kwargs):
    base = {
        "id": 1,
        "crm_provider": "demo",
        "crm_base_url": None,
        "crm_api_token": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_list_crm_staff_demo_source_for_none_provider():
    org = _org(crm_provider="none")
    session = _FakeSession(get_results={(crm_staff_service.Organization, 1): org})

    payload = await crm_staff_service.list_crm_staff(session, 1)

    assert payload["source"] == "demo"
    assert len(payload["items"]) > 0


@pytest.mark.asyncio
async def test_list_crm_staff_amocrm_without_cache_returns_hint():
    org = _org(crm_provider="amocrm", crm_base_url="https://crm.example", crm_api_token="secret")
    session = _FakeSession(
        get_results={
            (crm_staff_service.Organization, 1): org,
            (OrganizationCrmStaffCache, 1): None,
        }
    )

    payload = await crm_staff_service.list_crm_staff(session, 1)

    assert payload["source"] == "crm"
    assert payload["items"] == []
    assert payload.get("hint")


@pytest.mark.asyncio
async def test_sync_crm_staff_demo_skips_http():
    org = _org(crm_provider="demo")
    session = _FakeSession(get_results={(crm_staff_service.Organization, 1): org})

    payload = await crm_staff_service.sync_crm_staff(session, 1)

    assert payload["source"] == "demo"
    assert payload["count"] > 0
    assert session.committed is False


@pytest.mark.asyncio
async def test_crm_is_demo_treats_none_as_demo():
    org = _org(crm_provider="none", crm_base_url="https://crm.example", crm_api_token="x")
    assert crm_staff_service._crm_is_demo(org) is True


@pytest.mark.asyncio
async def test_list_crm_staff_yclients_without_cache_returns_hint():
    org = _org(
        crm_provider="yclients",
        crm_base_url=None,
        crm_api_token="partner-token",
        crm_config={"company_id": "4564"},
    )
    session = _FakeSession(
        get_results={
            (crm_staff_service.Organization, 1): org,
            (OrganizationCrmStaffCache, 1): None,
        }
    )

    payload = await crm_staff_service.list_crm_staff(session, 1)

    assert payload["source"] == "crm"
    assert payload["items"] == []
    assert payload.get("hint")


@pytest.mark.asyncio
async def test_sync_crm_staff_yclients_persists_cache(monkeypatch: pytest.MonkeyPatch):
    from bot.crm.base import StaffMember

    org = _org(
        crm_provider="yclients",
        crm_base_url=None,
        crm_api_token="partner-token",
        crm_config={"company_id": "4564"},
    )

    class _FakeProvider:
        async def list_staff(self):
            return [
                StaffMember(
                    id="16",
                    name="Dr YClients",
                    work_start="09:00",
                    work_end="18:00",
                    active=True,
                )
            ]

    session = _FakeSession(get_results={(crm_staff_service.Organization, 1): org})
    monkeypatch.setattr(crm_staff_service, "get_crm_provider", lambda _org: _FakeProvider())

    payload = await crm_staff_service.sync_crm_staff(session, 1)

    assert payload["source"] == "crm"
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == "16"
    assert session.committed is True
    assert len(session.added) == 1
    assert session.added[0].items[0]["name"] == "Dr YClients"

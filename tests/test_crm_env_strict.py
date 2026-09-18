from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import bot.crm.factory as factory
from bot.crm.amocrm import AmoCRMProvider
from bot.db.models import OrganizationCrmStaffCache
from bot.services import crm_staff_service


def _org(**kwargs):
    base = {
        "id": 1,
        "crm_provider": "amocrm",
        "crm_base_url": "https://crm.example",
        "crm_api_token": "org-secret",
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class _FakeSession:
    def __init__(self, get_results: dict):
        self._get_results = get_results

    async def get(self, model, key):
        return self._get_results.get((model, key))


@pytest.mark.asyncio
async def test_crm_staff_ignores_env_json_when_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "1")
    monkeypatch.setenv(
        "CRM_STAFF_JSON",
        json.dumps([{"id": "env-1", "name": "Env Doc", "work_start": "09:00", "work_end": "18:00"}]),
    )
    org = _org()
    cache = SimpleNamespace(
        organization_id=1,
        items=[{"id": "crm-42", "name": "Cached", "work_start": "10:00", "work_end": "19:00", "active": True}],
        synced_at=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
        source="crm",
        sync_error=None,
    )
    session = _FakeSession(
        get_results={
            (crm_staff_service.Organization, 1): org,
            (OrganizationCrmStaffCache, 1): cache,
        }
    )

    payload = await crm_staff_service.list_crm_staff(session, 1)

    assert payload["source"] == "crm"
    assert payload["items"][0]["id"] == "crm-42"


@pytest.mark.asyncio
async def test_crm_staff_uses_env_json_when_not_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv(
        "CRM_STAFF_JSON",
        json.dumps([{"id": "env-1", "name": "Env Doc", "work_start": "09:00", "work_end": "18:00"}]),
    )
    org = _org()
    session = _FakeSession(get_results={(crm_staff_service.Organization, 1): org})

    payload = await crm_staff_service.list_crm_staff(session, 1)

    assert payload["source"] == "config"
    assert payload["items"][0]["id"] == "env-1"


def test_factory_ignores_amocrm_env_when_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "1")
    monkeypatch.setenv("CRM_ENV_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("AMOCRM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("AMOCRM_TOKEN", "env-token")

    provider = factory.get_crm_provider(_org(crm_base_url=None, crm_api_token=None))

    assert provider.base_url == ""
    assert provider.token == ""
    assert provider.demo_mode is True


def test_factory_env_fallback_when_not_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv("CRM_ENV_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("AMOCRM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("AMOCRM_TOKEN", "env-token")

    provider = factory.get_crm_provider(_org(crm_base_url=None, crm_api_token=None))

    assert provider.base_url == "https://env.example.com"
    assert provider.token == "env-token"


def test_amocrm_provider_ignores_path_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "1")
    monkeypatch.setenv("AMOCRM_SLOTS_PATH", "/custom/slots")
    monkeypatch.setenv("AMOCRM_BOOKING_PATH", "/custom/book")
    monkeypatch.setenv("AMOCRM_STAFF_PATH", "/custom/staff")

    provider = AmoCRMProvider(base_url="https://crm.example", token="t")

    assert provider.slots_path == "/api/v4/appointments/slots"
    assert provider.booking_path == "/api/v4/appointments"
    assert provider.staff_path == "/api/v4/appointments/doctors"


def test_amocrm_provider_ignores_path_env_when_not_strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TENANT_CONFIG_STRICT", "0")
    monkeypatch.setenv("AMOCRM_STAFF_PATH", "/env/staff")

    provider = AmoCRMProvider(
        base_url="https://crm.example",
        token="t",
        config={"staff_path": "/cfg/staff"},
    )

    assert provider.staff_path == "/cfg/staff"

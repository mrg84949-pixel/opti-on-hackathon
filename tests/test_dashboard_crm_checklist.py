from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.db.models import Organization
from bot.services import stats_service


def _org(**kwargs):
    base = {
        "id": 1,
        "crm_provider": "amocrm",
        "crm_base_url": None,
        "crm_api_token": None,
        "system_prompt": "x" * 60,
        "whatsapp_provider": None,
        "whatsapp_instance_id": None,
        "whatsapp_meta_phone_number_id": None,
        "bot_enabled": True,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class _ScalarResult:
    def __init__(self, value=0):
        self._value = value

    def scalar_one(self):
        return self._value

    def all(self):
        return []


class _DashboardSession:
    def __init__(self, org: Organization):
        self._org = org

    async def get(self, model, key):
        if model is Organization and self._org.id == key:
            return self._org
        return None

    async def execute(self, _stmt):
        return _ScalarResult(0)


def test_crm_connected_demo_provider():
    assert stats_service._crm_connected(_org(crm_provider="demo")) is True


def test_crm_connected_amocrm_requires_credentials():
    assert stats_service._crm_connected(_org(crm_provider="amocrm")) is False
    assert stats_service._crm_connected(
        _org(crm_provider="amocrm", crm_base_url="https://crm.example", crm_api_token="tok")
    ) is True


def test_crm_connected_generic_rest_requires_credentials():
    assert stats_service._crm_connected(_org(crm_provider="generic_rest")) is False
    assert stats_service._crm_connected(
        _org(crm_provider="generic_rest", crm_base_url="https://rest.example", crm_api_token="tok")
    ) is True


def test_crm_connected_yclients_requires_credentials():
    assert stats_service._crm_connected(_org(crm_provider="yclients")) is False
    assert stats_service._crm_connected(
        _org(
            crm_provider="yclients",
            crm_api_token="partner-token",
            crm_config={"company_id": "4564"},
        )
    ) is True


@pytest.mark.asyncio
async def test_get_dashboard_summary_services_configured_true(monkeypatch: pytest.MonkeyPatch):
    org = Organization(id=1, name="Clinic")

    async def fake_business_stats(_session, _org_id):
        return {"total_customers": 0, "upcoming_appointments_30d": 0}

    async def fake_org_has_active_services(_session, _org_id):
        return True

    monkeypatch.setattr(stats_service, "get_business_stats", fake_business_stats)
    monkeypatch.setattr(stats_service, "org_has_active_services", fake_org_has_active_services)

    summary = await stats_service.get_dashboard_summary(_DashboardSession(org), 1)
    assert summary["setup"]["services_configured"] is True


@pytest.mark.asyncio
async def test_get_dashboard_summary_services_configured_false(monkeypatch: pytest.MonkeyPatch):
    org = Organization(id=1, name="Clinic")

    async def fake_business_stats(_session, _org_id):
        return {"total_customers": 0, "upcoming_appointments_30d": 0}

    async def fake_org_has_active_services(_session, _org_id):
        return False

    monkeypatch.setattr(stats_service, "get_business_stats", fake_business_stats)
    monkeypatch.setattr(stats_service, "org_has_active_services", fake_org_has_active_services)

    summary = await stats_service.get_dashboard_summary(_DashboardSession(org), 1)
    assert summary["setup"]["services_configured"] is False

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.llm.tools import _create_appointment_from_draft


def _demo_org(**overrides):
    base = {
        "id": 1,
        "timezone": "UTC",
        "crm_provider": "none",
        "crm_base_url": None,
        "crm_api_token": None,
        "auto_confirm_appointments": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_create_appointment_from_draft_snapshots_service_and_price(monkeypatch: pytest.MonkeyPatch):
    org = _demo_org()
    customer = SimpleNamespace(id=10, name="Alice", phone="+77001112233")
    draft = {
        "customer_name": "Alice",
        "service": "Консультация",
        "date": "2027-06-15",
        "time": "10:00",
        "doctor_id": "doc-1",
        "doctor_name": "Dr",
    }

    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()

    async def fake_auto_confirm(_session, _org, appt):
        return False

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

    monkeypatch.setattr(
        "bot.llm.tools.resolve_service_price_minor",
        AsyncMock(return_value=500_000),
    )
    monkeypatch.setattr("bot.llm.tools.appointment_service.auto_confirm_if_enabled", fake_auto_confirm)
    monkeypatch.setattr(
        "bot.llm.tools.notify_admins_about_new_appointment",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr("bot.services.appointment_service.get_crm_provider", lambda _org: _Provider())
    monkeypatch.setattr("bot.llm.tools.get_crm_provider", lambda _org: _Provider())

    async def fake_list_crm_staff(_session, _org_id):
        return {
            "source": "demo",
            "items": [{"id": "doc-1", "name": "Dr", "active": True}],
        }

    monkeypatch.setattr("bot.llm.tools.crm_staff_service.list_crm_staff", fake_list_crm_staff)

    async def fake_validate_slot(_session, _org, draft):
        draft.setdefault("doctor_id", "doc-1")
        draft.setdefault("doctor_name", "Dr")
        return None

    monkeypatch.setattr("bot.llm.tools._validate_draft_slot", fake_validate_slot)

    appt, _local_dt, auto_confirmed = await _create_appointment_from_draft(
        session,
        org=org,
        customer=customer,
        draft=draft,
    )

    assert auto_confirmed is False
    assert appt.service_name == "Консультация"
    assert appt.service_price_minor == 500_000
    session.add.assert_called_once()

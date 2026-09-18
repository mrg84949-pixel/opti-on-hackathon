from __future__ import annotations

import pytest

from bot.crm.amocrm import AmoCRMProvider


@pytest.mark.asyncio
async def test_amocrm_demo_slots_and_booking():
    provider = AmoCRMProvider(base_url=None, token=None)
    slots = await provider.get_available_slots("doc-1", "2026-05-01")
    assert len(slots) == 3
    booking = await provider.book_appointment("doc-1", "2026-05-01T10:00:00+00:00")
    assert booking.crm_appointment_id.startswith("demo-doc-1-")


@pytest.mark.asyncio
async def test_amocrm_demo_slots_sunday_empty():
    provider = AmoCRMProvider(base_url=None, token=None)
    slots = await provider.get_available_slots("doc-1", "2026-05-03")
    assert slots == []


@pytest.mark.asyncio
async def test_amocrm_live_slots_parsing(monkeypatch: pytest.MonkeyPatch):
    provider = AmoCRMProvider(base_url="https://crm.example.com", token="token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "slots": [
                    {"start": "2026-05-01T10:00:00", "end": "2026-05-01T10:30:00"},
                    {"start": "2026-05-01T11:00:00", "end": "2026-05-01T11:30:00"},
                ]
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            assert url == "https://crm.example.com/api/v4/appointments/slots"
            assert "Authorization" in headers
            assert params["doctor_id"] == "doc-1"
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.amocrm.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    slots = await provider.get_available_slots("doc-1", "2026-05-01")
    assert len(slots) == 2
    assert slots[0].start.hour == 10


@pytest.mark.asyncio
async def test_amocrm_list_recent_appointments_demo():
    provider = AmoCRMProvider(base_url=None, token=None)
    rows = await provider.list_recent_appointments(since_iso="2026-05-01T00:00:00+00:00")
    assert rows == []


@pytest.mark.asyncio
async def test_amocrm_list_recent_appointments_live(monkeypatch: pytest.MonkeyPatch):
    provider = AmoCRMProvider(base_url="https://crm.example.com", token="token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "appointments": [
                    {"id": "123", "status": "cancelled"},
                    {"id": "124", "status": "confirmed"},
                ]
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            assert url == "https://crm.example.com/api/v4/appointments"
            assert params == {"updated_since": "2026-05-01T00:00:00+00:00"}
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.amocrm.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    rows = await provider.list_recent_appointments(since_iso="2026-05-01T00:00:00+00:00")
    assert len(rows) == 2
    assert rows[0].crm_appointment_id == "123"
    assert rows[0].status == "cancelled"


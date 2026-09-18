from __future__ import annotations

import pytest

from bot.crm.generic_rest import GenericRestProvider


@pytest.mark.asyncio
async def test_generic_rest_demo_slots_and_booking():
    provider = GenericRestProvider(base_url=None, token=None)
    slots = await provider.get_available_slots("doc-1", "2026-05-01")
    assert len(slots) == 3
    booking = await provider.book_appointment("doc-1", "2026-05-01T10:00:00+00:00")
    assert booking.crm_appointment_id.startswith("demo-doc-1-")


@pytest.mark.asyncio
async def test_generic_rest_live_slots_parsing(monkeypatch: pytest.MonkeyPatch):
    provider = GenericRestProvider(base_url="https://api.clinic.example", token="token")

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
            assert url == "https://api.clinic.example/api/slots"
            assert headers == {"Authorization": "Bearer token"}
            assert params == {"doctor_id": "doc-1", "date": "2026-05-01"}
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.generic_rest.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    slots = await provider.get_available_slots("doc-1", "2026-05-01")
    assert len(slots) == 2


@pytest.mark.asyncio
async def test_generic_rest_live_booking(monkeypatch: pytest.MonkeyPatch):
    provider = GenericRestProvider(base_url="https://api.clinic.example", token="token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"appointment_id": "ext-42"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            assert url == "https://api.clinic.example/api/appointments"
            assert json["doctor_id"] == "doc-1"
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.generic_rest.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    booking = await provider.book_appointment("doc-1", "2026-05-01T10:00:00+00:00", customer_name="Ann")
    assert booking.crm_appointment_id == "ext-42"


@pytest.mark.asyncio
async def test_generic_rest_list_staff_live(monkeypatch: pytest.MonkeyPatch):
    provider = GenericRestProvider(base_url="https://api.clinic.example", token="token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"staff": [{"id": "s1", "name": "Dr A", "work_start": "09:00", "work_end": "18:00"}]}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None):
            assert url == "https://api.clinic.example/api/staff"
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.generic_rest.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    staff = await provider.list_staff()
    assert len(staff) == 1
    assert staff[0].id == "s1"
    assert staff[0].name == "Dr A"


def test_generic_rest_reads_paths_from_config():
    provider = GenericRestProvider(
        base_url="https://api.clinic.example",
        token="t",
        config={"staff_path": "/custom/staff", "slots_path": "/custom/slots"},
    )
    assert provider.staff_path == "/custom/staff"
    assert provider.slots_path == "/custom/slots"
    assert provider.booking_path == "/api/appointments"


@pytest.mark.asyncio
async def test_generic_rest_list_recent_appointments_live(monkeypatch: pytest.MonkeyPatch):
    provider = GenericRestProvider(base_url="https://api.clinic.example", token="token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"appointments": [{"appointment_id": "ext-9", "status": "cancelled"}]}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            assert url == "https://api.clinic.example/api/appointments"
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.generic_rest.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    rows = await provider.list_recent_appointments(since_iso="2026-05-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0].crm_appointment_id == "ext-9"

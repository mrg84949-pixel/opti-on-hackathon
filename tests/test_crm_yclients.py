from __future__ import annotations

import pytest

from bot.crm.yclients import YClientsProvider


@pytest.mark.asyncio
async def test_yclients_demo_slots_and_booking():
    provider = YClientsProvider(partner_token=None, company_id=None)
    assert provider.demo_mode is True
    slots = await provider.get_available_slots("42", "2026-05-01")
    assert len(slots) == 3
    booking = await provider.book_appointment("42", "2026-05-01T10:00:00+00:00", customer_name="Ann")
    assert booking.crm_appointment_id.startswith("demo-42-")


@pytest.mark.asyncio
async def test_yclients_demo_list_staff():
    provider = YClientsProvider(partner_token=None, company_id=None)
    staff = await provider.list_staff()
    assert len(staff) >= 1
    assert staff[0].id


@pytest.mark.asyncio
async def test_yclients_live_list_staff(monkeypatch: pytest.MonkeyPatch):
    provider = YClientsProvider(partner_token="partner", company_id="4564")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": [
                    {"id": 16, "name": "Vasya", "bookable": True},
                    {"id": 32, "name": "Petya", "bookable": False},
                ],
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            assert url == "https://api.yclients.com/api/v1/book_staff/4564"
            assert headers["Authorization"] == "Bearer partner"
            assert headers["Accept"] == "application/vnd.yclients.v2+json"
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.yclients.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    staff = await provider.list_staff()
    assert len(staff) == 2
    assert staff[0].name == "Vasya"
    assert staff[0].active is True
    assert staff[1].active is False


@pytest.mark.asyncio
async def test_yclients_live_get_available_slots(monkeypatch: pytest.MonkeyPatch):
    provider = YClientsProvider(partner_token="partner", company_id="4564", config={"default_service_id": "99"})

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": {
                    "seances": [
                        {"time": "10:00", "seance_length": 3600, "datetime": 1492063200},
                        {"time": "10:15", "seance_length": 1800, "datetime": 1492064100},
                    ]
                },
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            assert url == "https://api.yclients.com/api/v1/book_times/4564/16/2026-05-01"
            assert params == {"service_ids[]": 99}
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.yclients.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    slots = await provider.get_available_slots("16", "2026-05-01")
    assert len(slots) == 2
    assert slots[0].end > slots[0].start


@pytest.mark.asyncio
async def test_yclients_live_book_appointment(monkeypatch: pytest.MonkeyPatch):
    provider = YClientsProvider(
        partner_token="partner",
        company_id="4564",
        config={"default_service_id": "331"},
    )

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True, "data": [{"id": 1, "record_id": 9001, "record_hash": "abc"}]}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            assert url == "https://api.yclients.com/api/v1/book_record/4564"
            assert json["phone"] == "79161234567"
            assert json["fullname"] == "Ann"
            assert json["appointments"][0]["staff_id"] == 16
            assert json["appointments"][0]["services"] == [331]
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.yclients.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    booking = await provider.book_appointment(
        "16",
        "2026-05-01T10:00:00+00:00",
        customer_name="Ann",
        customer_phone="tg:79161234567",
    )
    assert booking.crm_appointment_id == "9001"


@pytest.mark.asyncio
async def test_yclients_list_recent_appointments_without_user_token():
    provider = YClientsProvider(partner_token="partner", company_id="4564")
    rows = await provider.list_recent_appointments(since_iso="2026-05-01T00:00:00+00:00")
    assert rows == []


@pytest.mark.asyncio
async def test_yclients_list_recent_appointments_live_deleted(monkeypatch: pytest.MonkeyPatch):
    provider = YClientsProvider(
        partner_token="partner",
        company_id="4564",
        user_token="user-token",
    )

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "success": True,
                "data": [
                    {"id": 100, "deleted": True},
                    {"id": 101, "deleted": False},
                    {"id": 102, "deleted": True},
                ],
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers=None, params=None):
            assert url == "https://api.yclients.com/api/v1/records/4564"
            assert headers["Authorization"] == "Bearer partner, User user-token"
            assert params["changed_after"] == "2026-05-01"
            assert params["with_deleted"] == 1
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.yclients.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    rows = await provider.list_recent_appointments(since_iso="2026-05-01T12:00:00+00:00")
    assert len(rows) == 2
    assert rows[0].crm_appointment_id == "100"
    assert rows[0].status == "cancelled"
    assert rows[1].crm_appointment_id == "102"

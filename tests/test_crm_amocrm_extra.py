from __future__ import annotations

import pytest

from bot.crm.amocrm import AmoCRMProvider


class _FakeResponse:
    def __init__(self, *, payload=None, error: Exception | None = None):
        self._payload = payload or {}
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, *, get_response=None, post_response=None):
        self.get_response = get_response
        self.post_response = post_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None, params=None):
        return self.get_response

    async def post(self, url, headers=None, json=None):
        return self.post_response


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"id": 123}, {"appointment_id": "appt-1"}])
async def test_amocrm_live_booking_success(monkeypatch: pytest.MonkeyPatch, payload):
    provider = AmoCRMProvider(base_url="https://crm.example.com", token="token")
    fake_client = _FakeClient(post_response=_FakeResponse(payload=payload))
    monkeypatch.setattr("bot.crm.amocrm.httpx.AsyncClient", lambda timeout=20: fake_client)
    booking = await provider.book_appointment("doc-1", "2026-05-01T10:00:00")
    assert booking.crm_appointment_id == str(payload.get("id") or payload.get("appointment_id"))


@pytest.mark.asyncio
async def test_amocrm_live_booking_missing_id_raises(monkeypatch: pytest.MonkeyPatch):
    provider = AmoCRMProvider(base_url="https://crm.example.com", token="token")
    fake_client = _FakeClient(post_response=_FakeResponse(payload={"status": "ok"}))
    monkeypatch.setattr("bot.crm.amocrm.httpx.AsyncClient", lambda timeout=20: fake_client)
    with pytest.raises(ValueError):
        await provider.book_appointment("doc-1", "2026-05-01T10:00:00")


@pytest.mark.asyncio
async def test_amocrm_live_slots_and_booking_raise_for_status(monkeypatch: pytest.MonkeyPatch):
    provider = AmoCRMProvider(base_url="https://crm.example.com", token="token")
    fake_get_client = _FakeClient(get_response=_FakeResponse(error=RuntimeError("slots failed")))
    monkeypatch.setattr("bot.crm.amocrm.httpx.AsyncClient", lambda timeout=20: fake_get_client)
    with pytest.raises(RuntimeError, match="slots failed"):
        await provider.get_available_slots("doc-1", "2026-05-01")

    fake_post_client = _FakeClient(post_response=_FakeResponse(error=RuntimeError("booking failed")))
    monkeypatch.setattr("bot.crm.amocrm.httpx.AsyncClient", lambda timeout=20: fake_post_client)
    with pytest.raises(RuntimeError, match="booking failed"):
        await provider.book_appointment("doc-1", "2026-05-01T10:00:00")

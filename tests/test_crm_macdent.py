from __future__ import annotations

import pytest

from bot.crm.macdent import MacDentError, MacDentProvider


@pytest.mark.asyncio
async def test_macdent_demo_slots_and_booking():
    provider = MacDentProvider(access_token=None)
    slots = await provider.get_available_slots("doc-1", "2026-05-01")
    assert len(slots) == 3
    booking = await provider.book_appointment("doc-1", "2026-05-01T10:00:00+00:00")
    assert booking.crm_appointment_id.startswith("demo-doc-1-")


@pytest.mark.asyncio
async def test_macdent_demo_list_staff_and_recent_appointments():
    provider = MacDentProvider(access_token=None)
    staff = await provider.list_staff()
    assert len(staff) >= 1
    recent = await provider.list_recent_appointments(since_iso="2026-05-01T00:00:00+00:00")
    assert recent == []


class _FakeSequentialPostClient:
    """book_appointment() now makes two POSTs in sequence (patient.add, then
    zapis.add) — respond per-URL so each step gets its own fixture."""

    def __init__(self, responses: dict[str, dict]):
        self._responses = responses
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, data=None):
        self.calls.append({"url": url, "data": data})
        response_json = self._responses[url]

        class _Resp:
            def raise_for_status(_self):
                return None

            def json(_self):
                return response_json

        return _Resp()


def _patient_add_url() -> str:
    return "https://api-developer.macdent.kz/patient/add"


def _zapis_add_url() -> str:
    return "https://api-developer.macdent.kz/zapis/add"


@pytest.mark.asyncio
async def test_macdent_book_appointment_creates_real_zapis(monkeypatch: pytest.MonkeyPatch):
    """book_appointment now maps to a two-step patient.add + zapis.add flow —
    confirmed live 2026-08-29 (real create + immediate zapis.remove round trip)."""
    provider = MacDentProvider(access_token="real-token", config={"filial": "200"})
    fake_client = _FakeSequentialPostClient(
        {
            _patient_add_url(): {"patient": {"id": 12263809}, "response": 1},
            _zapis_add_url(): {"zapis": {"id": 39558214}, "response": 1},
        }
    )
    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: fake_client)

    result = await provider.book_appointment(
        "23273", "2026-10-15T10:00:00", customer_name="Alice", customer_phone="tg:123456789"
    )

    assert result.crm_appointment_id == "39558214"
    assert len(fake_client.calls) == 2

    patient_call = fake_client.calls[0]
    assert patient_call["url"] == _patient_add_url()
    assert patient_call["data"]["name"] == "Alice"
    assert patient_call["data"]["phone"] == "123456789"  # tg: prefix stripped

    zapis_call = fake_client.calls[1]
    assert zapis_call["url"] == _zapis_add_url()
    body = zapis_call["data"]
    assert body["doctor"] == "23273"
    assert body["patient"] == "12263809"  # id from the patient.add response, not name/phone
    assert body["start"] == "15.10.2026 10:00:00"
    assert body["end"] == "15.10.2026 10:30:00"
    assert body["filial"] == "200"


@pytest.mark.asyncio
async def test_macdent_book_appointment_requires_filial_configured():
    """We refuse to guess a filial id — it's account/doctor-specific, not a
    safe default. Must fail before ever calling patient.add."""
    provider = MacDentProvider(access_token="real-token")  # no filial in config

    with pytest.raises(MacDentError, match="filial"):
        await provider.book_appointment("23273", "2026-10-15T10:00:00", customer_name="Alice")


@pytest.mark.asyncio
async def test_macdent_book_appointment_missing_name_falls_back(monkeypatch: pytest.MonkeyPatch):
    provider = MacDentProvider(access_token="real-token", config={"filial": "200"})
    fake_client = _FakeSequentialPostClient(
        {
            _patient_add_url(): {"patient": {"id": 1}, "response": 1},
            _zapis_add_url(): {"zapis": {"id": 1}, "response": 1},
        }
    )
    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: fake_client)

    await provider.book_appointment("23273", "2026-10-15T10:00:00", customer_name=None, customer_phone=None)

    patient_call = fake_client.calls[0]
    assert patient_call["data"]["name"] == "Клиент Optibot"
    assert patient_call["data"]["phone"] == "70000000000"


@pytest.mark.asyncio
async def test_macdent_book_appointment_patient_add_failure_raises(monkeypatch: pytest.MonkeyPatch):
    """Confirmed live: an empty/invalid body returns HTTP 200 with response=0,
    not an HTTP error status — must raise via the envelope check. zapis.add
    must never be attempted if patient.add itself failed."""
    provider = MacDentProvider(access_token="real-token", config={"filial": "200"})
    fake_client = _FakeSequentialPostClient(
        {_patient_add_url(): {"error": "Имя пациента должно быть указано", "response": 0}}
    )
    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: fake_client)

    with pytest.raises(MacDentError, match="Имя пациента должно быть указано"):
        await provider.book_appointment("23273", "2026-10-15T10:00:00", customer_name="", customer_phone="")

    assert len(fake_client.calls) == 1  # never reached zapis.add


@pytest.mark.asyncio
async def test_macdent_book_appointment_missing_zapis_id_raises(monkeypatch: pytest.MonkeyPatch):
    provider = MacDentProvider(access_token="real-token", config={"filial": "200"})
    fake_client = _FakeSequentialPostClient(
        {
            _patient_add_url(): {"patient": {"id": 1}, "response": 1},
            _zapis_add_url(): {"zapis": {}, "response": 1},
        }
    )
    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: fake_client)

    with pytest.raises(MacDentError, match="missing zapis.id"):
        await provider.book_appointment("23273", "2026-10-15T10:00:00")


def test_macdent_uses_fixed_base_url_not_configurable():
    provider = MacDentProvider(access_token="tok")
    assert provider.base_url == "https://api-developer.macdent.kz"


@pytest.mark.asyncio
async def test_macdent_auth_is_query_param_not_header(monkeypatch: pytest.MonkeyPatch):
    """MacDent auth is access_token as a URL query param (confirmed live),
    not a Bearer header like the other providers."""
    provider = MacDentProvider(access_token="real-token")
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": 1, "doctors": []}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    await provider.list_staff()
    assert captured["url"] == "https://api-developer.macdent.kz/doctor/find"
    assert captured["params"]["access_token"] == "real-token"


@pytest.mark.asyncio
async def test_macdent_parses_ddmmyyyy_slots_and_filters_by_date(monkeypatch: pytest.MonkeyPatch):
    """Confirmed live response shape: {"schedules": [{"from": "12.08.2026 13:00:00", ...}]}."""
    provider = MacDentProvider(access_token="real-token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "response": 1,
                "schedules": [
                    {"from": "12.08.2026 13:00:00", "to": "12.08.2026 14:00:00", "filial": 200},
                    {"from": "13.08.2026 09:00:00", "to": "13.08.2026 10:00:00", "filial": 200},
                ],
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    slots = await provider.get_available_slots("23273", "2026-08-12")
    assert len(slots) == 1
    assert slots[0].start.isoformat() == "2026-08-12T13:00:00"
    assert slots[0].end.isoformat() == "2026-08-12T14:00:00"


@pytest.mark.asyncio
async def test_macdent_list_staff_parses_real_doctor_find_shape(monkeypatch: pytest.MonkeyPatch):
    """Confirmed live response shape: {"doctors": [{"id", "name", "specialnosti", "filials"}]} —
    no work_start/work_end fields, unlike amoCRM/generic_rest."""
    provider = MacDentProvider(access_token="real-token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "response": 1,
                "doctors": [
                    {
                        "id": 23273,
                        "name": "Иванов Иван Иванович",
                        "specialnosti": [{"name": "Терапия", "id": "9010"}],
                        "filials": [200],
                    }
                ],
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    staff = await provider.list_staff()
    assert len(staff) == 1
    assert staff[0].id == "23273"
    assert staff[0].name == "Иванов Иван Иванович"


@pytest.mark.asyncio
async def test_macdent_response_zero_envelope_raises_not_http_status(monkeypatch: pytest.MonkeyPatch):
    """Confirmed live: MacDent returns HTTP 200 with {"response": 0, "error": ...}
    for failures (e.g. missing permission) — raise_for_status() alone can't see this."""
    provider = MacDentProvider(access_token="real-token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"error": "Нет прав appointments_read", "response": 0}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    # list_staff/list_recent_appointments swallow MacDentError into [] (logged),
    # so assert the underlying _get raises directly.
    with pytest.raises(MacDentError, match="appointments_read"):
        await provider._get("/doctor/find")


@pytest.mark.asyncio
async def test_macdent_list_staff_returns_empty_on_permission_error(monkeypatch: pytest.MonkeyPatch):
    provider = MacDentProvider(access_token="real-token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"error": "Нет прав doctors_read", "response": 0}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    assert await provider.list_staff() == []


@pytest.mark.asyncio
async def test_macdent_list_recent_appointments_returns_empty_on_missing_permission(
    monkeypatch: pytest.MonkeyPatch,
):
    provider = MacDentProvider(access_token="real-token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"error": "Нет прав appointments_read", "response": 0}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    rows = await provider.list_recent_appointments(since_iso="2026-05-01T00:00:00+00:00")
    assert rows == []


@pytest.mark.asyncio
async def test_macdent_list_recent_appointments_parses_real_zapisi_shape(monkeypatch: pytest.MonkeyPatch):
    """Confirmed live (2026-08-21, zapis_read granted): the list key is
    "zapisi", not "zapis"/"items" — a plausible-looking guess that turned out
    wrong until verified against a real response."""
    provider = MacDentProvider(access_token="real-token")

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "zapisi": [
                    {"id": 9001, "status": "DECLINED"},
                    {"id": 9002, "status": "CONFIRM"},
                ],
                "count": "18911",
                "atPage": 100,
                "maxPage": 190,
                "response": 1,
            }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            return _FakeResponse()

    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    rows = await provider.list_recent_appointments(since_iso="2026-05-01T00:00:00+00:00")
    assert len(rows) == 2
    assert rows[0].crm_appointment_id == "9001"
    assert rows[0].status == "declined"


@pytest.mark.asyncio
async def test_macdent_http_error_message_never_contains_token(monkeypatch: pytest.MonkeyPatch):
    """The access_token lives in the query string — any exception surfaced to
    generic logging call sites must not embed the raw request URL."""
    import httpx

    provider = MacDentProvider(access_token="super-secret-token")

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params=None):
            request = httpx.Request("GET", url, params=params)
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("boom", request=request, response=response)

    monkeypatch.setattr("bot.crm.macdent.httpx.AsyncClient", lambda timeout=20: _FakeClient())
    with pytest.raises(MacDentError) as excinfo:
        await provider._get("/doctor/find")
    assert "super-secret-token" not in str(excinfo.value)

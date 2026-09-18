from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.services import whatsapp_org_service as wa_org


def _org(**kwargs):
    base = {
        "id": 1,
        "whatsapp_instance_id": None,
        "whatsapp_meta_phone_number_id": None,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class _FakeHttpResponse:
    def __init__(self, *, status_code: int = 200, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text
        self.is_success = 200 <= status_code < 300

    def json(self):
        return self._json_data


class _FakeHttpClient:
    def __init__(self, response: _FakeHttpResponse):
        self._response = response
        self.last_url: str | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, **kwargs):
        self.last_url = url
        return self._response


@pytest.mark.asyncio
async def test_test_whatsapp_connection_routes_to_green(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    green = AsyncMock(return_value=wa_org.WhatsAppConnectionTestResult(ok=True, provider="green", message="ok"))
    monkeypatch.setattr(wa_org, "resolve_whatsapp_provider", lambda _org: "green")
    monkeypatch.setattr(wa_org, "_test_green_connection", green)
    result = await wa_org.test_whatsapp_connection(org)
    assert result.ok is True
    green.assert_awaited_once_with(org)


@pytest.mark.asyncio
async def test_test_whatsapp_connection_routes_to_meta(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    meta = AsyncMock(return_value=wa_org.WhatsAppConnectionTestResult(ok=True, provider="meta", message="ok"))
    monkeypatch.setattr(wa_org, "resolve_whatsapp_provider", lambda _org: "meta")
    monkeypatch.setattr(wa_org, "_test_meta_connection", meta)
    result = await wa_org.test_whatsapp_connection(org)
    assert result.provider == "meta"
    meta.assert_awaited_once_with(org)


@pytest.mark.asyncio
async def test_green_connection_missing_credentials(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    monkeypatch.setattr(wa_org, "green_instance_id", lambda _org: "")
    monkeypatch.setattr(wa_org, "green_api_token", lambda _org: "")
    result = await wa_org._test_green_connection(org)
    assert result.ok is False
    assert "не настроены" in result.message


@pytest.mark.asyncio
async def test_green_connection_authorized(monkeypatch: pytest.MonkeyPatch):
    org = _org(whatsapp_instance_id="99")
    monkeypatch.setattr(wa_org, "green_instance_id", lambda _org: "99")
    monkeypatch.setattr(wa_org, "green_api_token", lambda _org: "tok")
    client = _FakeHttpClient(_FakeHttpResponse(json_data={"stateInstance": "authorized"}))
    monkeypatch.setattr(wa_org.httpx, "AsyncClient", lambda timeout=20.0: client)
    result = await wa_org._test_green_connection(org)
    assert result.ok is True
    assert "authorized" in result.message.lower()


@pytest.mark.asyncio
async def test_green_connection_bad_state(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    monkeypatch.setattr(wa_org, "green_instance_id", lambda _org: "1")
    monkeypatch.setattr(wa_org, "green_api_token", lambda _org: "tok")
    client = _FakeHttpClient(_FakeHttpResponse(json_data={"stateInstance": "notAuthorized"}))
    monkeypatch.setattr(wa_org.httpx, "AsyncClient", lambda timeout=20.0: client)
    result = await wa_org._test_green_connection(org)
    assert result.ok is False


@pytest.mark.asyncio
async def test_green_connection_auth_http_error(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    monkeypatch.setattr(wa_org, "green_instance_id", lambda _org: "1")
    monkeypatch.setattr(wa_org, "green_api_token", lambda _org: "tok")
    client = _FakeHttpClient(_FakeHttpResponse(status_code=401, text="unauthorized"))
    monkeypatch.setattr(wa_org.httpx, "AsyncClient", lambda timeout=20.0: client)
    result = await wa_org._test_green_connection(org)
    assert result.ok is False
    assert "token" in result.message.lower()


@pytest.mark.asyncio
async def test_green_connection_network_error(monkeypatch: pytest.MonkeyPatch):
    org = _org()

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            raise RuntimeError("network down")

    monkeypatch.setattr(wa_org, "green_instance_id", lambda _org: "1")
    monkeypatch.setattr(wa_org, "green_api_token", lambda _org: "tok")
    monkeypatch.setattr(wa_org.httpx, "AsyncClient", lambda timeout=20.0: _BoomClient())
    result = await wa_org._test_green_connection(org)
    assert result.ok is False
    assert "network" in result.message


@pytest.mark.asyncio
async def test_meta_connection_missing_credentials(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    monkeypatch.setattr(wa_org, "meta_phone_number_id", lambda _org: "")
    monkeypatch.setattr(wa_org, "meta_access_token", lambda _org: "")
    result = await wa_org._test_meta_connection(org)
    assert result.ok is False
    assert "не настроены" in result.message


@pytest.mark.asyncio
async def test_meta_connection_success(monkeypatch: pytest.MonkeyPatch):
    org = _org(whatsapp_meta_phone_number_id="pn-1")
    monkeypatch.setattr(wa_org, "meta_phone_number_id", lambda _org: "pn-1")
    monkeypatch.setattr(wa_org, "meta_access_token", lambda _org: "meta-tok")
    client = _FakeHttpClient(
        _FakeHttpResponse(json_data={"display_phone_number": "+7700", "verified_name": "Clinic"})
    )
    monkeypatch.setattr(wa_org.httpx, "AsyncClient", lambda timeout=20.0: client)
    result = await wa_org._test_meta_connection(org)
    assert result.ok is True
    assert result.display_phone == "+7700"


@pytest.mark.asyncio
async def test_meta_connection_invalid_token(monkeypatch: pytest.MonkeyPatch):
    org = _org()
    monkeypatch.setattr(wa_org, "meta_phone_number_id", lambda _org: "pn-1")
    monkeypatch.setattr(wa_org, "meta_access_token", lambda _org: "bad")
    client = _FakeHttpClient(_FakeHttpResponse(status_code=401, text="OAuthException"))
    monkeypatch.setattr(wa_org.httpx, "AsyncClient", lambda timeout=20.0: client)
    result = await wa_org._test_meta_connection(org)
    assert result.ok is False
    assert "token" in result.message.lower()

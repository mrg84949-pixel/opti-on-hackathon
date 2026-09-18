from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.api.whatsapp as whatsapp_api
from bot.channels.whatsapp import outbound
from bot.services import notification_service
from bot.services import telegram_org_service as tg_org


def _org(**overrides):
    base = {
        "id": 1,
        "telegram_bot_token": None,
        "whatsapp_provider": None,
        "whatsapp_instance_id": None,
        "whatsapp_api_token": None,
        "whatsapp_meta_phone_number_id": None,
        "whatsapp_meta_access_token": None,
        "whatsapp_meta_reminder_template_name": None,
        "whatsapp_meta_reminder_template_lang": "ru",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeHttpClient:
    def __init__(self):
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return SimpleNamespace(status_code=200, is_success=True)


@pytest.fixture
def strict_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.telegram_token", "env-tg")
    monkeypatch.setattr("bot.config.settings.green_api_instance_id", "env-inst")
    monkeypatch.setattr("bot.config.settings.green_api_token", "env-green")
    monkeypatch.setattr("bot.config.settings.whatsapp_phone_number_id", "env-pnid")
    monkeypatch.setattr("bot.config.settings.whatsapp_graph_access_token", "env-meta")


@pytest.fixture
def dev_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    monkeypatch.setattr("bot.config.settings.telegram_token", "env-tg-token")
    monkeypatch.setattr("bot.config.settings.green_api_instance_id", "env-inst")
    monkeypatch.setattr("bot.config.settings.green_api_token", "env-green")


@pytest.mark.asyncio
async def test_wa_green_send_skips_env_when_strict(strict_mode, monkeypatch: pytest.MonkeyPatch):
    called = False

    async def fake_green_send(**_kwargs):
        nonlocal called
        called = True
        return True, None, False

    monkeypatch.setattr(outbound.green_api, "send_message", fake_green_send)
    ok = await outbound.send_whatsapp_text(_org(whatsapp_provider="green"), "777@c.us", "hello")
    assert ok.ok is False
    assert called is False


@pytest.mark.asyncio
async def test_wa_meta_send_skips_env_when_strict(strict_mode, monkeypatch: pytest.MonkeyPatch):
    called = False

    async def fake_meta_send(**_kwargs):
        nonlocal called
        called = True
        return True, None, False

    monkeypatch.setattr(outbound.meta_cloud, "send_text_message", fake_meta_send)
    ok = await outbound.send_whatsapp_text(_org(whatsapp_provider="meta"), "77771112233", "hello")
    assert ok.ok is False
    assert called is False


@pytest.mark.asyncio
async def test_wa_template_skips_env_when_strict(strict_mode):
    ok = await outbound.send_whatsapp_template(
        _org(whatsapp_provider="meta"),
        "77771112233",
        template_name="reminder",
        language_code="ru",
        body_parameters=["hi"],
    )
    assert ok.ok is False


@pytest.mark.asyncio
async def test_tg_send_skips_env_when_strict(strict_mode, monkeypatch: pytest.MonkeyPatch):
    called = False

    class _ShouldNotRun:
        async def __aenter__(self):
            nonlocal called
            called = True
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("bot.services.telegram_org_service.httpx.AsyncClient", lambda timeout=20: _ShouldNotRun())
    ok = await tg_org.send_telegram_for_org(_org(), 777, "hello")
    assert ok.ok is False
    assert called is False


@pytest.mark.asyncio
async def test_notification_tg_skips_env_when_strict(strict_mode, monkeypatch: pytest.MonkeyPatch):
    called = False

    class _ShouldNotRun:
        async def __aenter__(self):
            nonlocal called
            called = True
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr("bot.services.telegram_org_service.httpx.AsyncClient", lambda timeout=20: _ShouldNotRun())
    ok = await notification_service.send_customer_message(
        _org(),
        SimpleNamespace(phone="tg:777"),
        "reminder",
    )
    assert ok.ok is False
    assert called is False


@pytest.mark.asyncio
async def test_notification_wa_skips_env_when_strict(strict_mode, monkeypatch: pytest.MonkeyPatch):
    called = False

    async def fake_green_send(**_kwargs):
        nonlocal called
        called = True
        return True, None, False

    monkeypatch.setattr(outbound.green_api, "send_message", fake_green_send)
    ok = await notification_service.send_customer_message(
        _org(whatsapp_provider="green"),
        SimpleNamespace(phone="wa:777@c.us"),
        "reminder",
    )
    assert ok.ok is False
    assert called is False


@pytest.mark.asyncio
async def test_legacy_send_whatsapp_message_strict_requires_org_id(strict_mode, monkeypatch: pytest.MonkeyPatch):
    called = False

    class _Session:
        async def get(self, _model, _key):
            nonlocal called
            called = True
            return _org(whatsapp_provider="green", whatsapp_instance_id="1", whatsapp_api_token="t")

    class _SessionManager:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(whatsapp_api, "AsyncSessionLocal", lambda: _SessionManager())
    await whatsapp_api.send_whatsapp_message("777@c.us", "hello", org_id=None)
    assert called is False


@pytest.mark.asyncio
async def test_dev_env_fallback_for_tg_send(dev_mode, monkeypatch: pytest.MonkeyPatch):
    client = _FakeHttpClient()
    monkeypatch.setattr("bot.services.telegram_org_service.httpx.AsyncClient", lambda timeout=20: client)
    ok = await tg_org.send_telegram_for_org(_org(), 777, "hello")
    assert ok.ok is True
    assert "botenv-tg-token/sendMessage" in client.calls[0]["url"]


@pytest.mark.asyncio
async def test_dev_env_fallback_for_wa_green_send(dev_mode, monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, str] = {}

    async def fake_green_send(**kwargs):
        captured.update(kwargs)
        return True, None, False

    monkeypatch.setattr(outbound.green_api, "send_message", fake_green_send)
    ok = await outbound.send_whatsapp_text(_org(whatsapp_provider="green"), "777@c.us", "hello")
    assert ok.ok is True
    assert captured["instance_id"] == "env-inst"
    assert captured["api_token"] == "env-green"

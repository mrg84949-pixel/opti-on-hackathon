from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import bot.api.whatsapp as whatsapp_api
from bot.channels.whatsapp import green_api, meta_cloud, outbound


def _test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(whatsapp_api.router, prefix="/bot")
    return app


async def _always_claim(*_args, **_kwargs):
    return True


class _FakeHTTPResponse:
    def __init__(self, *, status_code: int = 200, text: str = "ok"):
        self.status_code = status_code
        self.text = text

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _FakeAsyncClient:
    def __init__(self, response: _FakeHTTPResponse):
        self.response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.response


def _org(**overrides):
    base = {
        "id": 1,
        "whatsapp_provider": None,
        "whatsapp_instance_id": None,
        "whatsapp_api_token": None,
        "whatsapp_meta_phone_number_id": None,
        "whatsapp_meta_access_token": None,
        "whatsapp_meta_reminder_template_name": None,
        "whatsapp_meta_reminder_template_lang": "ru",
        "whatsapp_broadcast_quota_monthly": 100,
        "whatsapp_broadcast_sent_count": 0,
        "whatsapp_broadcast_month_key": None,
        "whatsapp_broadcast_locked": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _allow_unsigned_meta_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dev-mode Meta webhook: no signature required."""
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.delenv("WHATSAPP_VERIFY_TOKEN", raising=False)
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    monkeypatch.setattr("bot.config.settings.whatsapp_app_secret", "")
    monkeypatch.setattr("bot.config.settings.whatsapp_verify_token", "")


@pytest.mark.asyncio
async def test_meta_verify_success_and_fail(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "expected")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get(
            "/bot/whatsapp/meta",
            params={"hub.mode": "subscribe", "hub.verify_token": "expected", "hub.challenge": "abc123"},
        )
        bad = await client.get(
            "/bot/whatsapp/meta",
            params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "abc123"},
        )
    assert ok.status_code == 200
    assert ok.text == "abc123"
    assert bad.status_code == 403


@pytest.mark.asyncio
async def test_meta_webhook_rejects_invalid_signature(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "secret")
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    monkeypatch.setattr("bot.config.settings.whatsapp_app_secret", "secret")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/bot/whatsapp/meta",
            content=json.dumps({"entry": []}).encode("utf-8"),
            headers={"x-hub-signature-256": "sha256=wrong"},
        )
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid signature"


@pytest.mark.asyncio
async def test_meta_webhook_strict_rejects_without_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "expected")
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", True)
    monkeypatch.setattr("bot.config.settings.whatsapp_verify_token", "expected")
    monkeypatch.setattr("bot.config.settings.whatsapp_app_secret", "")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/bot/whatsapp/meta",
            content=json.dumps({"entry": []}).encode("utf-8"),
        )
    assert response.status_code == 403
    assert "signature verification required" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_meta_webhook_rejects_without_secret_when_verify_token_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "expected")
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    monkeypatch.setattr("bot.config.settings.whatsapp_verify_token", "expected")
    monkeypatch.setattr("bot.config.settings.whatsapp_app_secret", "")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/bot/whatsapp/meta",
            content=json.dumps({"entry": []}).encode("utf-8"),
        )
    assert response.status_code == 403
    assert "signature verification required" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_meta_webhook_invalid_json(monkeypatch: pytest.MonkeyPatch):
    _allow_unsigned_meta_webhook(monkeypatch)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/bot/whatsapp/meta", content=b"{bad json")
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid JSON"


@pytest.mark.asyncio
async def test_meta_webhook_ignores_non_text(monkeypatch: pytest.MonkeyPatch):
    _allow_unsigned_meta_webhook(monkeypatch)
    app = _test_app()
    payload = {
        "entry": [
            {
                "changes": [
                    {"value": {"metadata": {"phone_number_id": "pnid"}, "messages": [{"type": "image", "from": "777"}]}}
                ]
            }
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/bot/whatsapp/meta", content=json.dumps(payload).encode("utf-8"))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_meta_webhook_org_not_found(monkeypatch: pytest.MonkeyPatch):
    _allow_unsigned_meta_webhook(monkeypatch)

    async def fake_resolve_org(_phone_number_id):
        return None

    monkeypatch.setattr(whatsapp_api, "_resolve_org_meta", fake_resolve_org)
    app = _test_app()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "pnid"},
                            "messages": [{"type": "text", "from": "777777", "text": {"body": "hello"}}],
                        }
                    }
                ]
            }
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/bot/whatsapp/meta", content=json.dumps(payload).encode("utf-8"))
    assert response.status_code == 200
    assert response.json()["detail"] == "org_not_found"


@pytest.mark.asyncio
async def test_meta_webhook_success_normalizes_user_and_replies(monkeypatch: pytest.MonkeyPatch):
    _allow_unsigned_meta_webhook(monkeypatch)
    captured: dict[str, object] = {}
    fake_org = _org(id=7, whatsapp_provider="meta")

    async def fake_resolve_org(phone_number_id):
        captured["phone_number_id"] = phone_number_id
        return fake_org

    async def fake_llm(*, user_id, user_text, db_memory, channel, org_id):
        captured["user_id"] = user_id
        captured["user_text"] = user_text
        captured["channel"] = channel
        captured["org_id"] = org_id
        return "meta-reply"

    async def fake_send(org, chat_id, text):
        captured["sent_org"] = org
        captured["sent_chat_id"] = chat_id
        captured["sent_text"] = text
        return True

    monkeypatch.setattr(whatsapp_api, "_resolve_org_meta", fake_resolve_org)
    monkeypatch.setattr(whatsapp_api, "claim_inbound_event_or_duplicate", _always_claim)
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)
    monkeypatch.setattr(whatsapp_api, "send_whatsapp_text", fake_send)
    app = _test_app()
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "pnid-1"},
                            "messages": [
                                {
                                    "type": "text",
                                    "id": "wamid.test-1",
                                    "from": "+7 (777) 111-22-33",
                                    "text": {"body": "hello"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/bot/whatsapp/meta", content=json.dumps(payload).encode("utf-8"))
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert captured["user_id"] == "77771112233@c.us"
    assert captured["channel"] == "whatsapp"
    assert captured["org_id"] == 7
    assert captured["sent_org"] is fake_org
    assert captured["sent_chat_id"] == "77771112233@c.us"
    assert captured["sent_text"] == "meta-reply"


@pytest.mark.asyncio
async def test_green_webhook_invalid_payload(monkeypatch: pytest.MonkeyPatch):
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/bot/whatsapp/webhook", json={"senderData": {}, "messageData": {}})
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_green_webhook_org_not_found(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve_org(_instance_hint):
        return None

    monkeypatch.setattr(whatsapp_api, "_resolve_org_green", fake_resolve_org)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/bot/whatsapp/webhook",
            json={
                "senderData": {"chatId": "777@c.us"},
                "messageData": {"textMessageData": {"textMessage": "hello"}},
            },
        )
    assert response.status_code == 200
    assert response.json()["detail"] == "org_not_found"


class _GreenResolveScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _GreenResolveSession:
    def __init__(self, org):
        self.org = org

    async def execute(self, stmt):
        crit = stmt.whereclause
        field = crit.left.key
        value = crit.right.value
        if field == "whatsapp_instance_id" and getattr(self.org, field, None) == value:
            return _GreenResolveScalarResult(self.org)
        return _GreenResolveScalarResult(None)


class _GreenResolveSessionManager:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.asyncio
async def test_green_webhook_unknown_instance_no_llm(monkeypatch: pytest.MonkeyPatch):
    """SG-01: real _resolve_org_green — unknown instanceId must not fall back to DEFAULT_ORG_ID."""
    demo_org = _org(id=1, whatsapp_instance_id="42", whatsapp_provider="green", whatsapp_api_token="t")
    llm_calls: list[dict] = []

    async def fake_llm(**kwargs):
        llm_calls.append(kwargs)
        return "should-not-run"

    monkeypatch.setattr(
        whatsapp_api,
        "AsyncSessionLocal",
        lambda: _GreenResolveSessionManager(_GreenResolveSession(demo_org)),
    )
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)

    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/bot/whatsapp/webhook",
            json={
                "instanceId": "99",
                "senderData": {"chatId": "777@c.us"},
                "messageData": {"textMessageData": {"textMessage": "hello"}},
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert body["detail"] == "org_not_found"
    assert llm_calls == []


@pytest.mark.asyncio
async def test_green_webhook_exception_returns_error(monkeypatch: pytest.MonkeyPatch):
    async def fake_resolve_org(_instance_hint):
        return _org(whatsapp_provider="green", whatsapp_instance_id="1", whatsapp_api_token="t")

    async def fake_llm(**_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(whatsapp_api, "_resolve_org_green", fake_resolve_org)
    monkeypatch.setattr(whatsapp_api, "claim_inbound_event_or_duplicate", _always_claim)
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/bot/whatsapp/webhook",
            json={
                "idMessage": "green-msg-1",
                "senderData": {"chatId": "777@c.us"},
                "messageData": {"textMessageData": {"textMessage": "hello"}},
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert "boom" in response.json()["detail"]


@pytest.mark.asyncio
async def test_meta_duplicate_skips_llm(monkeypatch: pytest.MonkeyPatch):
    _allow_unsigned_meta_webhook(monkeypatch)
    claim_calls = 0
    llm_calls = 0

    async def fake_claim(*_args, **_kwargs):
        nonlocal claim_calls
        claim_calls += 1
        return claim_calls == 1

    async def fake_resolve_org(_phone_number_id):
        return _org(id=7, whatsapp_provider="meta")

    async def fake_llm(**_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "meta-reply"

    monkeypatch.setattr(whatsapp_api, "_resolve_org_meta", fake_resolve_org)
    async def fake_send(*_args, **_kwargs):
        return True

    monkeypatch.setattr(whatsapp_api, "claim_inbound_event_or_duplicate", fake_claim)
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)
    monkeypatch.setattr(whatsapp_api, "send_whatsapp_text", fake_send)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "pnid-1"},
                            "messages": [
                                {
                                    "type": "text",
                                    "id": "wamid.dup-1",
                                    "from": "777",
                                    "text": {"body": "hello"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/bot/whatsapp/meta", content=json.dumps(payload).encode("utf-8"))
        second = await client.post("/bot/whatsapp/meta", content=json.dumps(payload).encode("utf-8"))

    assert first.status_code == 200
    assert first.json()["status"] == "ok"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert llm_calls == 1


@pytest.mark.asyncio
async def test_green_duplicate_skips_llm(monkeypatch: pytest.MonkeyPatch):
    claim_calls = 0
    llm_calls = 0

    async def fake_claim(*_args, **_kwargs):
        nonlocal claim_calls
        claim_calls += 1
        return claim_calls == 1

    async def fake_resolve_org(_instance_hint):
        return _org(whatsapp_provider="green", whatsapp_instance_id="1", whatsapp_api_token="t")

    async def fake_llm(**_kwargs):
        nonlocal llm_calls
        llm_calls += 1
        return "green-reply"

    monkeypatch.setattr(whatsapp_api, "_resolve_org_green", fake_resolve_org)
    async def fake_send(*_args, **_kwargs):
        return True

    monkeypatch.setattr(whatsapp_api, "claim_inbound_event_or_duplicate", fake_claim)
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)
    monkeypatch.setattr(whatsapp_api, "send_whatsapp_text", fake_send)

    payload = {
        "idMessage": "green-dup-1",
        "senderData": {"chatId": "777@c.us"},
        "messageData": {"textMessageData": {"textMessage": "hello"}},
    }
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/bot/whatsapp/webhook", json=payload)
        second = await client.post("/bot/whatsapp/webhook", json=payload)

    assert first.status_code == 200
    assert first.json()["status"] == "ok"
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert llm_calls == 1


@pytest.mark.asyncio
async def test_send_whatsapp_message_skips_when_org_missing(monkeypatch: pytest.MonkeyPatch):
    class _Session:
        async def get(self, _model, _key):
            return None

    class _SessionManager:
        async def __aenter__(self):
            return _Session()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(whatsapp_api, "AsyncSessionLocal", lambda: _SessionManager())
    await whatsapp_api.send_whatsapp_message("777@c.us", "hello", org_id=999)


def test_meta_cloud_helpers_and_signature(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "21.0")
    assert meta_cloud.graph_api_version() == "v21.0"
    assert meta_cloud.graph_messages_url("123") == "https://graph.facebook.com/v21.0/123/messages"

    secret = "topsecret"
    raw = b'{"entry":[]}'
    digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    assert meta_cloud.verify_signature_raw(secret, raw, f"sha256={digest}") is True
    assert meta_cloud.verify_signature_raw(secret, raw, "sha256=wrong") is False
    assert meta_cloud.verify_signature_raw("", raw, None) is False


@pytest.mark.asyncio
async def test_meta_cloud_send_text_and_template(monkeypatch: pytest.MonkeyPatch):
    ok_client = _FakeAsyncClient(_FakeHTTPResponse(status_code=200, text="ok"))
    fail_client = _FakeAsyncClient(_FakeHTTPResponse(status_code=400, text="bad request"))

    monkeypatch.setattr(meta_cloud.httpx, "AsyncClient", lambda timeout=30: ok_client)
    ok, err, auth_failed = await meta_cloud.send_text_message(
        phone_number_id="pnid",
        access_token="token",
        to_e164_or_chat="+7 (777) 111-22-33",
        text="x" * 5000,
    )
    assert ok is True and err is None and auth_failed is False
    assert ok_client.calls[0]["json"]["to"] == "77771112233"
    assert len(ok_client.calls[0]["json"]["text"]["body"]) == 4096

    monkeypatch.setattr(meta_cloud.httpx, "AsyncClient", lambda timeout=30: fail_client)
    ok2, err2, auth_failed2 = await meta_cloud.send_template_message(
        phone_number_id="pnid",
        access_token="token",
        to_e164_or_chat="777@c.us",
        template_name="promo_tpl",
        language_code="ru",
        body_parameters=["one", "two"],
    )
    assert ok2 is False
    assert err2 == "bad request"
    assert auth_failed2 is False
    payload = fail_client.calls[0]["json"]
    assert payload["template"]["name"] == "promo_tpl"
    assert payload["template"]["components"][0]["parameters"][1]["text"] == "two"


@pytest.mark.asyncio
async def test_meta_cloud_send_text_empty_recipient():
    ok, err, auth_failed = await meta_cloud.send_text_message(
        phone_number_id="pnid",
        access_token="token",
        to_e164_or_chat="",
        text="hello",
    )
    assert ok is False
    assert err == "empty_recipient"
    assert auth_failed is False


@pytest.mark.asyncio
async def test_green_api_send_message_success_and_failure(monkeypatch: pytest.MonkeyPatch):
    ok_client = _FakeAsyncClient(_FakeHTTPResponse(status_code=200, text="ok"))
    monkeypatch.setattr(green_api.httpx, "AsyncClient", lambda timeout=25: ok_client)
    ok, err, auth_failed = await green_api.send_message(instance_id="1", api_token="t", chat_id="777@c.us", text="x" * 13000)
    assert ok is True and err is None and auth_failed is False
    assert len(ok_client.calls[0]["json"]["message"]) == 12000

    fail_client = _FakeAsyncClient(_FakeHTTPResponse(status_code=500, text="server error"))
    monkeypatch.setattr(green_api.httpx, "AsyncClient", lambda timeout=25: fail_client)
    ok2, err2, auth_failed2 = await green_api.send_message(instance_id="1", api_token="t", chat_id="777@c.us", text="hello")
    assert ok2 is False
    assert err2 == "server error"
    assert auth_failed2 is False


def test_outbound_provider_resolution_and_helpers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("bot.config.settings.tenant_config_strict", False)
    assert outbound.resolve_whatsapp_provider(_org(whatsapp_provider="meta")) == "meta"
    assert outbound.resolve_whatsapp_provider(_org(whatsapp_provider="green")) == "green"
    assert outbound.resolve_whatsapp_provider(
        _org(whatsapp_meta_phone_number_id="pnid", whatsapp_meta_access_token="meta-token")
    ) == "meta"
    assert outbound.resolve_whatsapp_provider(_org(whatsapp_instance_id="1", whatsapp_api_token="g-token")) == "green"
    monkeypatch.setattr(outbound.settings, "whatsapp_provider", "meta")
    assert outbound.resolve_whatsapp_provider(_org()) == "meta"
    monkeypatch.setattr(outbound.settings, "whatsapp_graph_access_token", "env-meta")
    monkeypatch.setattr(outbound.settings, "whatsapp_phone_number_id", "env-pnid")
    assert outbound.meta_access_token(_org()) == "env-meta"
    assert outbound.meta_phone_number_id(_org()) == "env-pnid"


@pytest.mark.asyncio
async def test_outbound_send_text_meta_and_green(monkeypatch: pytest.MonkeyPatch):
    called: dict[str, object] = {}

    async def fake_meta_send(**kwargs):
        called["meta"] = kwargs
        return True, None, False

    async def fake_green_send(**kwargs):
        called["green"] = kwargs
        return True, None, False

    monkeypatch.setattr(outbound.meta_cloud, "send_text_message", fake_meta_send)
    monkeypatch.setattr(outbound.green_api, "send_message", fake_green_send)

    meta_org = _org(
        id=2,
        whatsapp_provider="meta",
        whatsapp_meta_phone_number_id="pnid",
        whatsapp_meta_access_token="meta-token",
    )
    green_org = _org(
        id=3,
        whatsapp_provider="green",
        whatsapp_instance_id="1",
        whatsapp_api_token="g-token",
    )
    assert (await outbound.send_whatsapp_text(meta_org, "77771112233", "hello")).ok
    assert called["meta"]["phone_number_id"] == "pnid"
    assert (await outbound.send_whatsapp_text(green_org, "777@c.us", "hello")).ok
    assert called["green"]["instance_id"] == "1"


@pytest.mark.asyncio
async def test_outbound_send_text_and_template_missing_config(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GREEN_API_INSTANCE_ID", raising=False)
    monkeypatch.delenv("GREEN_API_TOKEN", raising=False)
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID", raising=False)
    monkeypatch.delenv("WHATSAPP_GRAPH_ACCESS_TOKEN", raising=False)

    assert not (await outbound.send_whatsapp_text(_org(id=10, whatsapp_provider="meta"), "777", "hello")).ok
    assert not (await outbound.send_whatsapp_text(_org(id=11, whatsapp_provider="green"), "777@c.us", "hello")).ok
    assert not (
        await outbound.send_whatsapp_template(
            _org(id=12),
            "77771112233",
            template_name="tpl",
            language_code="ru",
            body_parameters=[],
        )
    ).ok


@pytest.mark.asyncio
async def test_outbound_send_template_success(monkeypatch: pytest.MonkeyPatch):
    called: dict[str, object] = {}

    async def fake_template_send(**kwargs):
        called["kwargs"] = kwargs
        return True, None, False

    monkeypatch.setattr(outbound.meta_cloud, "send_template_message", fake_template_send)
    org = _org(id=3, whatsapp_meta_phone_number_id="pnid", whatsapp_meta_access_token="token")
    ok = await outbound.send_whatsapp_template(
        org,
        "77771112233",
        template_name="reminder_tpl",
        language_code="ru",
        body_parameters=["hello"],
    )
    assert ok.ok is True
    assert called["kwargs"]["template_name"] == "reminder_tpl"

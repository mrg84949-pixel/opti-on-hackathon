"""Wave 2 C.4: Meta WhatsApp webhook — verify, inbound LLM, unknown phone."""
from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

import bot.api.whatsapp as whatsapp_api
from test_whatsapp_stack import (
    _allow_unsigned_meta_webhook,
    _always_claim,
    _org,
    _test_app,
)


@pytest.mark.asyncio
async def test_c4_meta_verify_challenge(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "c4-expected")
    monkeypatch.setattr("bot.config.settings.whatsapp_verify_token", "c4-expected")
    app = _test_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.get(
            "/bot/whatsapp/meta",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "c4-expected",
                "hub.challenge": "challenge-c4",
            },
        )
        bad = await client.get(
            "/bot/whatsapp/meta",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "challenge-c4",
            },
        )
    assert ok.status_code == 200
    assert ok.text == "challenge-c4"
    assert bad.status_code == 403


@pytest.mark.asyncio
async def test_c4_meta_inbound_llm_outbound(monkeypatch: pytest.MonkeyPatch):
    _allow_unsigned_meta_webhook(monkeypatch)
    captured: dict[str, object] = {}
    fake_org = _org(id=7, whatsapp_provider="meta", whatsapp_meta_phone_number_id="pnid-c4")

    async def fake_resolve_org(phone_number_id):
        captured["phone_number_id"] = phone_number_id
        return fake_org

    async def fake_llm(*, user_id, user_text, db_memory, channel, org_id):
        captured["user_id"] = user_id
        captured["user_text"] = user_text
        captured["channel"] = channel
        captured["org_id"] = org_id
        return "c4-meta-reply"

    async def fake_send(org, chat_id, text):
        captured["sent_org"] = org
        captured["sent_chat_id"] = chat_id
        captured["sent_text"] = text
        return True

    monkeypatch.setattr(whatsapp_api, "_resolve_org_meta", fake_resolve_org)
    monkeypatch.setattr(whatsapp_api, "claim_inbound_event_or_duplicate", _always_claim)
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)
    monkeypatch.setattr(whatsapp_api, "send_whatsapp_text", fake_send)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "pnid-c4"},
                            "messages": [
                                {
                                    "type": "text",
                                    "id": "wamid.c4-inbound-1",
                                    "from": "+7 (777) 111-22-33",
                                    "text": {"body": "hello c4"},
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
        response = await client.post(
            "/bot/whatsapp/meta",
            content=json.dumps(payload).encode("utf-8"),
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert captured["user_id"] == "77771112233@c.us"
    assert captured["channel"] == "whatsapp"
    assert captured["org_id"] == 7
    assert captured["sent_text"] == "c4-meta-reply"


@pytest.mark.asyncio
async def test_c4_meta_unknown_phone_no_cross_talk(monkeypatch: pytest.MonkeyPatch):
    _allow_unsigned_meta_webhook(monkeypatch)
    llm_called = {"ok": False}
    send_called = {"ok": False}

    async def fake_resolve_org(_phone_number_id):
        return None

    async def fake_llm(**_kwargs):
        llm_called["ok"] = True
        return "should-not-send"

    async def fake_send(*_args, **_kwargs):
        send_called["ok"] = True
        return True

    monkeypatch.setattr(whatsapp_api, "_resolve_org_meta", fake_resolve_org)
    monkeypatch.setattr(whatsapp_api, "get_ai_response", fake_llm)
    monkeypatch.setattr(whatsapp_api, "send_whatsapp_text", fake_send)

    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "unknown-pn-c4"},
                            "messages": [
                                {
                                    "type": "text",
                                    "id": "wamid.c4-unknown",
                                    "from": "79990001122",
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
        response = await client.post(
            "/bot/whatsapp/meta",
            content=json.dumps(payload).encode("utf-8"),
        )
    assert response.status_code == 200
    assert response.json()["detail"] == "org_not_found"
    assert llm_called["ok"] is False
    assert send_called["ok"] is False

from __future__ import annotations

import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from bot.channels.whatsapp import meta_cloud
from bot.channels.whatsapp.outbound import send_whatsapp_text
from bot.config import settings, tenant_env_fallback_allowed
from bot.db.database import AsyncSessionLocal
from bot.db.models import Organization
from bot.llm.llm_engine import get_ai_response
from bot.logging_config import get_logger
from bot.services.rate_limit import get_limiter
from bot.services.webhook_dedup import (
    CHANNEL_WA_GREEN,
    CHANNEL_WA_META,
    claim_inbound_event_or_duplicate,
    extract_green_wa_event_key,
    extract_meta_wa_event_key,
)

logger = get_logger(__name__)

router = APIRouter()


def _normalize_wa_user_id(raw: str) -> str:
    """Единый идентификатор чата Green/Meta: <digits>@c.us"""
    digits = "".join(c for c in (raw or "") if c.isdigit())
    return f"{digits}@c.us" if digits else ""


async def _wa_sender_rate_limited(org_id: int, sender_key: str) -> bool:
    """Per-sender (not per-IP) cap on LLM-triggering messages — protects
    spend from one number flooding the bot, independent of the generic
    per-IP webhook limit. Silent: caller must skip the LLM call and reply
    with nothing, not an error message (temporary ignore, not a rejection)."""
    if not settings.rate_limit_enabled:
        return False
    limit = settings.rate_limit_whatsapp_sender_limit
    if limit <= 0:
        return False
    key = f"wa_sender:{org_id}:{sender_key}"
    retry_after = await get_limiter().check(
        key, limit=limit, window_seconds=settings.rate_limit_whatsapp_sender_window_seconds
    )
    return retry_after is not None


async def _resolve_org_green(instance_hint: str | None) -> Organization | None:
    hint = (instance_hint or "").strip()
    if not hint:
        logger.warning(
            "Green webhook: no instance hint",
            extra={"extra_data": {"event": "wa_resolve_green_no_hint"}},
        )
        return None
    async with AsyncSessionLocal() as session:
        stmt = select(Organization).where(Organization.whatsapp_instance_id == hint)
        org = (await session.execute(stmt)).scalar_one_or_none()
        if org is None:
            logger.warning(
                "Green webhook: unknown instance",
                extra={"extra_data": {"event": "wa_resolve_green_unknown_instance", "instance_hint": hint}},
            )
        return org


async def _resolve_org_meta(phone_number_id: str | None) -> Organization | None:
    pid = (phone_number_id or "").strip()
    if not pid:
        logger.warning(
            "Meta webhook: no phone_number_id",
            extra={"extra_data": {"event": "wa_resolve_meta_no_phone_number_id"}},
        )
        return None
    async with AsyncSessionLocal() as session:
        stmt = select(Organization).where(Organization.whatsapp_meta_phone_number_id == pid)
        org = (await session.execute(stmt)).scalar_one_or_none()
        if org is None:
            logger.warning(
                "Meta webhook: unknown phone_number_id",
                extra={"extra_data": {"event": "wa_resolve_meta_unknown_phone_number_id", "phone_number_id": pid}},
            )
        return org


async def send_whatsapp_message(chat_id: str, text: str, *, org_id: int | None = None) -> None:
    """Обратная совместимость: отправка в организацию по org_id или DEFAULT_ORG_ID."""
    if org_id is None and not tenant_env_fallback_allowed():
        logger.warning(
            "WhatsApp send skipped: org_id required in tenant_config_strict",
            extra={"extra_data": {"event": "wa_send_skipped_no_org_id"}},
        )
        return
    oid = org_id if org_id is not None else settings.default_org_id
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, oid)
        if org is None:
            logger.warning(
                "WhatsApp send skipped: org not found",
                extra={"extra_data": {"event": "wa_send_skipped_no_org", "org_id": oid}},
            )
            return
        await send_whatsapp_text(org, chat_id, text)


@router.get("/whatsapp/meta")
async def whatsapp_meta_verify(request: Request):
    """Подписка webhook Meta Cloud API (challenge)."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    expected = settings.whatsapp_verify_token
    if mode == "subscribe" and token and challenge and expected and token == expected:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp/meta")
async def whatsapp_meta_webhook(request: Request):
    raw = await request.body()
    verify_configured = bool((settings.whatsapp_verify_token or "").strip())
    secret = (settings.whatsapp_app_secret or "").strip()
    if verify_configured and not secret:
        raise HTTPException(
            status_code=403,
            detail="WhatsApp Meta webhook signature verification required",
        )
    if secret:
        sig = request.headers.get("x-hub-signature-256")
        if not meta_cloud.verify_signature_raw(secret, raw, sig):
            raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON") from None

    phone_number_id: str | None = None
    from_user: str | None = None
    text_body = ""

    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            meta = value.get("metadata") or {}
            phone_number_id = meta.get("phone_number_id") or phone_number_id
            for msg in value.get("messages") or []:
                mtype = (msg.get("type") or "").lower()
                if mtype != "text":
                    continue
                from_user = msg.get("from") or from_user
                text_body = ((msg.get("text") or {}).get("body") or "").strip()

    if not from_user or not text_body:
        return {"status": "ignored", "reason": "non-text or empty"}

    org = await _resolve_org_meta(phone_number_id)
    if org is None:
        logger.error(
            "Meta webhook: organization not resolved",
            extra={"extra_data": {"event": "wa_meta_no_org", "phone_number_id": phone_number_id}},
        )
        return {"status": "error", "detail": "org_not_found"}

    event_key = extract_meta_wa_event_key(data)
    async with AsyncSessionLocal() as session:
        if not await claim_inbound_event_or_duplicate(
            session, channel=CHANNEL_WA_META, event_key=event_key
        ):
            return {"status": "duplicate"}
        await session.commit()

    wa_uid = _normalize_wa_user_id(from_user)
    if await _wa_sender_rate_limited(org.id, wa_uid):
        logger.info(
            "WhatsApp sender rate limited, skipping LLM call",
            extra={"extra_data": {"event": "wa_sender_rate_limited", "org_id": org.id}},
        )
        return {"status": "rate_limited"}
    ai_reply = await get_ai_response(
        user_id=wa_uid,
        user_text=text_body,
        db_memory={},
        channel="whatsapp",
        org_id=org.id,
    )
    await send_whatsapp_text(org, wa_uid, ai_reply)
    return {"status": "ok"}


@router.post("/whatsapp/webhook")
async def whatsapp_green_webhook(request: Request):
    """
    Green-API webhook endpoint (legacy).
    Принимает входящие сообщения WhatsApp, запрашивает ответ у LLM и отправляет обратно.
    """
    try:
        data = await request.json()

        message_data = data.get("messageData") or {}
        text_data = message_data.get("textMessageData") or {}
        user_text = (text_data.get("textMessage") or "").strip()
        sender_chat_id = data.get("senderData", {}).get("chatId")
        instance_hint = str(
            data.get("instanceId") or data.get("idInstance") or data.get("instance_id") or ""
        ).strip()

        logger.info(
            "WhatsApp Green webhook received",
            extra={
                "extra_data": {
                    "event": "wa_webhook_green_in",
                    "has_text": bool(user_text),
                    "has_sender": bool(sender_chat_id),
                }
            },
        )

        if not sender_chat_id or not user_text:
            return {"status": "ignored", "reason": "non-text or invalid payload"}

        org = await _resolve_org_green(instance_hint or None)
        if org is None:
            return {"status": "error", "detail": "org_not_found"}

        event_key = extract_green_wa_event_key(data)
        async with AsyncSessionLocal() as session:
            if not await claim_inbound_event_or_duplicate(
                session, channel=CHANNEL_WA_GREEN, event_key=event_key
            ):
                return {"status": "duplicate"}
            await session.commit()

        if await _wa_sender_rate_limited(org.id, str(sender_chat_id)):
            logger.info(
                "WhatsApp sender rate limited, skipping LLM call",
                extra={"extra_data": {"event": "wa_sender_rate_limited", "org_id": org.id}},
            )
            return {"status": "rate_limited"}
        ai_reply = await get_ai_response(
            user_id=str(sender_chat_id),
            user_text=user_text,
            db_memory={},
            channel="whatsapp",
            org_id=org.id,
        )
        await send_whatsapp_text(org, str(sender_chat_id), ai_reply)
        return {"status": "ok"}
    except Exception as e:
        logger.exception(
            "WhatsApp webhook error",
            extra={"extra_data": {"event": "wa_webhook_error", "error_type": type(e).__name__}},
        )
        return {"status": "error", "detail": str(e)}

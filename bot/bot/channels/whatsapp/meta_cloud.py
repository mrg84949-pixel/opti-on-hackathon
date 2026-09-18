from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from bot.config import settings
from bot.logging_config import get_logger
from bot.services.outbound_result import is_auth_http_status

logger = get_logger(__name__)


def graph_api_version() -> str:
    v = settings.whatsapp_graph_api_version.strip()
    return v if v.startswith("v") else f"v{v}"


def graph_messages_url(phone_number_id: str) -> str:
    return f"https://graph.facebook.com/{graph_api_version()}/{phone_number_id}/messages"


def verify_signature_raw(app_secret: str, raw_body: bytes, signature_header: str | None) -> bool:
    if not app_secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    digest = signature_header.split("=", 1)[1].strip()
    expected = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, digest)


def _digits_only(phone: str) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())


async def send_text_message(
    *,
    phone_number_id: str,
    access_token: str,
    to_e164_or_chat: str,
    text: str,
) -> tuple[bool, str | None, bool]:
    """Отправка обычного текстового сообщения (окно 24ч или ответ на входящее)."""
    url = graph_messages_url(phone_number_id)
    to_digits = _digits_only(to_e164_or_chat)
    if not to_digits:
        return False, "empty_recipient", False
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_digits,
        "type": "text",
        "text": {"body": text[:4096]},
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.is_success:
            return True, None, False
        body = response.text[:800]
        auth_failed = is_auth_http_status(response.status_code)
        logger.warning(
            "Meta WhatsApp text send failed",
            extra={"extra_data": {"event": "meta_wa_send_fail", "status": response.status_code, "body": body}},
        )
        return False, body, auth_failed


async def send_template_message(
    *,
    phone_number_id: str,
    access_token: str,
    to_e164_or_chat: str,
    template_name: str,
    language_code: str,
    body_parameters: list[str],
) -> tuple[bool, str | None, bool]:
    """Отправка по одобренному шаблону (напоминания, рассылки вне 24ч)."""
    url = graph_messages_url(phone_number_id)
    to_digits = _digits_only(to_e164_or_chat)
    if not to_digits:
        return False, "empty_recipient", False
    components: list[dict[str, Any]] = []
    if body_parameters:
        components.append(
            {
                "type": "body",
                "parameters": [{"type": "text", "text": p[:1024]} for p in body_parameters],
            }
        )
    template_block: dict[str, Any] = {
        "name": template_name,
        "language": {"code": language_code},
    }
    if components:
        template_block["components"] = components
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "to": to_digits,
        "type": "template",
        "template": template_block,
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, json=payload, headers=headers)
        if response.is_success:
            return True, None, False
        body = response.text[:800]
        auth_failed = is_auth_http_status(response.status_code)
        logger.warning(
            "Meta WhatsApp template send failed",
            extra={"extra_data": {"event": "meta_wa_template_fail", "status": response.status_code, "body": body}},
        )
        return False, body, auth_failed

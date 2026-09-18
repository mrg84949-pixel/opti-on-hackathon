from __future__ import annotations

from dataclasses import dataclass

import httpx

from bot.channels.whatsapp.meta_cloud import graph_api_version
from bot.channels.whatsapp.outbound import (
    green_api_token,
    green_instance_id,
    meta_access_token,
    meta_phone_number_id,
    resolve_whatsapp_provider,
)
from bot.db.models import Organization
from bot.logging_config import get_logger
from bot.services.outbound_result import is_auth_http_status

logger = get_logger(__name__)


@dataclass(frozen=True)
class WhatsAppConnectionTestResult:
    ok: bool
    provider: str
    message: str
    display_phone: str | None = None


async def test_whatsapp_connection(org: Organization) -> WhatsAppConnectionTestResult:
    provider = resolve_whatsapp_provider(org)
    if provider == "meta":
        return await _test_meta_connection(org)
    return await _test_green_connection(org)


async def _test_green_connection(org: Organization) -> WhatsAppConnectionTestResult:
    instance = (green_instance_id(org) or "").strip()
    token = (green_api_token(org) or "").strip()
    if not instance or not token:
        return WhatsAppConnectionTestResult(
            ok=False,
            provider="green",
            message="Green-API instance id или token не настроены для этой организации.",
        )
    url = f"https://api.green-api.com/waInstance{instance}/getStateInstance/{token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url)
        if response.is_success:
            payload = response.json()
            state = str(payload.get("stateInstance") or "").lower()
            if state == "authorized":
                return WhatsAppConnectionTestResult(
                    ok=True,
                    provider="green",
                    message="Green-API: инстанс authorized.",
                )
            return WhatsAppConnectionTestResult(
                ok=False,
                provider="green",
                message=f"Green-API state: {state or 'unknown'}.",
            )
        auth_failed = is_auth_http_status(response.status_code)
        detail = response.text[:300]
        return WhatsAppConnectionTestResult(
            ok=False,
            provider="green",
            message=f"Green-API HTTP {response.status_code}: {detail}" if not auth_failed else "Green-API: неверный token.",
        )
    except Exception as exc:
        logger.warning(
            "WhatsApp Green test failed",
            extra={
                "extra_data": {
                    "event": "whatsapp_test_green_failed",
                    "org_id": org.id,
                    "error": str(exc)[:200],
                }
            },
        )
        return WhatsAppConnectionTestResult(
            ok=False,
            provider="green",
            message=str(exc)[:300],
        )


async def _test_meta_connection(org: Organization) -> WhatsAppConnectionTestResult:
    phone_number_id = (meta_phone_number_id(org) or "").strip()
    token = (meta_access_token(org) or "").strip()
    if not phone_number_id or not token:
        return WhatsAppConnectionTestResult(
            ok=False,
            provider="meta",
            message="Meta phone_number_id или access token не настроены для этой организации.",
        )
    url = f"https://graph.facebook.com/{graph_api_version()}/{phone_number_id}"
    params = {"fields": "display_phone_number,verified_name"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(url, params=params, headers=headers)
        if response.is_success:
            payload = response.json()
            display = (payload.get("display_phone_number") or "").strip() or None
            verified = (payload.get("verified_name") or "").strip()
            label = display or verified or phone_number_id
            return WhatsAppConnectionTestResult(
                ok=True,
                provider="meta",
                message=f"Meta Cloud: {label}.",
                display_phone=display,
            )
        auth_failed = is_auth_http_status(response.status_code)
        detail = response.text[:300]
        return WhatsAppConnectionTestResult(
            ok=False,
            provider="meta",
            message=f"Meta HTTP {response.status_code}: {detail}" if not auth_failed else "Meta: неверный access token.",
        )
    except Exception as exc:
        logger.warning(
            "WhatsApp Meta test failed",
            extra={
                "extra_data": {
                    "event": "whatsapp_test_meta_failed",
                    "org_id": org.id,
                    "error": str(exc)[:200],
                }
            },
        )
        return WhatsAppConnectionTestResult(
            ok=False,
            provider="meta",
            message=str(exc)[:300],
        )

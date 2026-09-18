from __future__ import annotations

from bot.channels.whatsapp import green_api, meta_cloud  # re-exported for test monkeypatching
from bot.channels.whatsapp.base import WhatsAppTransport
from bot.channels.whatsapp.transports import GreenApiTransport, MetaCloudTransport
from bot.config import resolve_tenant_outbound_secret, settings, tenant_env_fallback_allowed
from bot.db.models import Organization
from bot.services.org_secrets import get_org_secret, secret_is_set
from bot.services.outbound_result import OutboundSendResult
from bot.logging_config import get_logger

logger = get_logger(__name__)


def resolve_whatsapp_provider(org: Organization) -> str:
    """meta | green — приоритет явного поля организации, иначе эвристика и WHATSAPP_PROVIDER."""
    stored = (org.whatsapp_provider or "").strip().lower()
    if stored in ("meta", "green"):
        return stored
    meta_phone = (org.whatsapp_meta_phone_number_id or "").strip()
    meta_tok = meta_access_token(org)
    if meta_phone and meta_tok:
        return "meta"
    green_ok = (org.whatsapp_instance_id or "").strip() and secret_is_set(org, "whatsapp_api_token")
    if green_ok:
        return "green"
    if not tenant_env_fallback_allowed():
        return "green"
    env_p = settings.whatsapp_provider.strip().lower()
    return env_p if env_p in ("meta", "green") else "green"


def meta_phone_number_id(org: Organization) -> str:
    return resolve_tenant_outbound_secret(
        org.whatsapp_meta_phone_number_id,
        settings.whatsapp_phone_number_id,
    )


def meta_access_token(org: Organization) -> str:
    return resolve_tenant_outbound_secret(
        get_org_secret(org, "whatsapp_meta_access_token"),
        settings.whatsapp_graph_access_token,
    )


def green_instance_id(org: Organization) -> str:
    return resolve_tenant_outbound_secret(
        org.whatsapp_instance_id,
        settings.green_api_instance_id,
    )


def green_api_token(org: Organization) -> str:
    return resolve_tenant_outbound_secret(
        get_org_secret(org, "whatsapp_api_token"),
        settings.green_api_token,
    )


def build_whatsapp_transport(org: Organization) -> WhatsAppTransport | None:
    """Resolve which transport this org's outbound WhatsApp sends should use.
    Adding a new provider (e.g. an official-WABA BSP) means one more branch
    here and a new WhatsAppTransport implementation — nothing else in the
    codebase needs to change. Returns None when the resolved provider is
    missing required credentials."""
    provider = resolve_whatsapp_provider(org)
    if provider == "meta":
        pnid = meta_phone_number_id(org)
        tok = meta_access_token(org)
        if not pnid or not tok:
            return None
        return MetaCloudTransport(phone_number_id=pnid, access_token=tok)
    instance = green_instance_id(org)
    token = green_api_token(org)
    if not instance or not token:
        return None
    return GreenApiTransport(instance_id=instance, api_token=token)


async def send_whatsapp_text(org: Organization, chat_id: str, text: str) -> OutboundSendResult:
    """Исходящее текстовое сообщение через активный транспорт org (Green/Meta/...).
    chat_id для Green — id чата; для Meta — номер WhatsApp."""
    transport = build_whatsapp_transport(org)
    if transport is None:
        logger.warning(
            "WhatsApp send skipped: transport not configured",
            extra={
                "extra_data": {
                    "event": "wa_send_skipped_no_transport",
                    "org_id": org.id,
                    "provider": resolve_whatsapp_provider(org),
                }
            },
        )
        return OutboundSendResult.skipped("whatsapp")
    result = await transport.send_text(chat_id=chat_id, text=text)
    if result.ok:
        return OutboundSendResult.success("whatsapp")
    if result.auth_failed:
        return OutboundSendResult.auth_error("whatsapp")
    return OutboundSendResult.retryable_error("whatsapp")


async def send_whatsapp_template(
    org: Organization,
    to_phone: str,
    *,
    template_name: str,
    language_code: str,
    body_parameters: list[str],
) -> OutboundSendResult:
    """Шаблонные сообщения — WABA-only (Green API не поддерживает шаблоны,
    поэтому это не часть общего WhatsAppTransport)."""
    pnid = meta_phone_number_id(org)
    tok = meta_access_token(org)
    if not pnid or not tok:
        return OutboundSendResult.skipped("whatsapp")
    transport = MetaCloudTransport(phone_number_id=pnid, access_token=tok)
    result = await transport.send_template(
        to=to_phone,
        template_name=template_name,
        language_code=language_code,
        body_parameters=body_parameters,
    )
    if result.ok:
        return OutboundSendResult.success("whatsapp")
    if result.auth_failed:
        return OutboundSendResult.auth_error("whatsapp")
    return OutboundSendResult.retryable_error("whatsapp")

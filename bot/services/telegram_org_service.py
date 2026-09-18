from __future__ import annotations

import hmac

from dataclasses import dataclass

import httpx
from sqlalchemy import select

from bot.config import resolve_tenant_outbound_secret, settings
from bot.db.database import AsyncSessionLocal
from bot.db.models import Organization
from bot.services.org_secrets import get_org_secret
from bot.services.secret_encryption import hash_secret
from bot.services.outbound_result import OutboundSendResult, telegram_response_auth_failed
from bot.logging_config import get_logger

logger = get_logger(__name__)


def expected_telegram_webhook_url() -> str | None:
    explicit = (settings.telegram_webhook_url or "").strip()
    if explicit:
        return explicit
    base = (settings.backend_public_url or "").strip().rstrip("/")
    if base:
        return f"{base}/bot/webhook"
    return None


@dataclass(frozen=True)
class TelegramConnectionTestResult:
    ok: bool
    token_set: bool
    bot_username: str | None
    webhook_url: str | None
    expected_webhook_url: str | None
    message: str


async def _telegram_api_get(token: str, method: str, **params: str) -> dict:
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"https://api.telegram.org/bot{token}/{method}",
            params=params or None,
        )
        response.raise_for_status()
        payload = response.json()
    if not payload.get("ok"):
        description = str(payload.get("description") or "Telegram API error")
        raise RuntimeError(description)
    return payload.get("result") or {}


def _telegram_test_result_from_webhook(
    *,
    token_set: bool,
    bot_username: str | None,
    webhook_url: str | None,
    expected: str | None,
    ok: bool,
    message: str,
) -> TelegramConnectionTestResult:
    return TelegramConnectionTestResult(
        ok=ok,
        token_set=token_set,
        bot_username=bot_username,
        webhook_url=webhook_url,
        expected_webhook_url=expected,
        message=message,
    )


async def test_telegram_connection(org: Organization) -> TelegramConnectionTestResult:
    token = (get_org_secret(org, "telegram_bot_token") or "").strip()
    expected = expected_telegram_webhook_url()
    if not token:
        return TelegramConnectionTestResult(
            ok=False,
            token_set=False,
            bot_username=None,
            webhook_url=None,
            expected_webhook_url=expected,
            message="Telegram bot token is not configured for this organization.",
        )
    try:
        me = await _telegram_api_get(token, "getMe")
        username = me.get("username")
        bot_username = f"@{username}" if username else None
        webhook_info = await _telegram_api_get(token, "getWebhookInfo")
        webhook_url = (webhook_info.get("url") or "").strip() or None
        if webhook_url:
            if expected and webhook_url.rstrip("/") == expected.rstrip("/"):
                message = f"Подключение успешно: {bot_username or 'bot'}, webhook настроен."
            else:
                message = f"Токен валиден ({bot_username or 'bot'}), webhook: {webhook_url}."
        else:
            message = f"Токен валиден ({bot_username or 'bot'}), webhook не зарегистрирован."
        return TelegramConnectionTestResult(
            ok=True,
            token_set=True,
            bot_username=bot_username,
            webhook_url=webhook_url,
            expected_webhook_url=expected,
            message=message,
        )
    except Exception as exc:
        logger.warning(
            "Telegram test connection failed",
            extra={
                "extra_data": {
                    "event": "org_integrations_test_telegram_failed",
                    "org_id": org.id,
                    "error": str(exc)[:200],
                }
            },
        )
        return TelegramConnectionTestResult(
            ok=False,
            token_set=True,
            bot_username=None,
            webhook_url=None,
            expected_webhook_url=expected,
            message=str(exc)[:300],
        )


async def register_telegram_webhook(org: Organization) -> TelegramConnectionTestResult:
    token = (get_org_secret(org, "telegram_bot_token") or "").strip()
    expected = expected_telegram_webhook_url()
    if not token:
        return _telegram_test_result_from_webhook(
            token_set=False,
            bot_username=None,
            webhook_url=None,
            expected=expected,
            ok=False,
            message="Telegram bot token is not configured for this organization.",
        )
    if not expected:
        return _telegram_test_result_from_webhook(
            token_set=True,
            bot_username=None,
            webhook_url=None,
            expected=None,
            ok=False,
            message="Expected webhook URL is not configured (TELEGRAM_WEBHOOK_URL or BACKEND_PUBLIC_URL).",
        )
    try:
        params: dict[str, str] = {
            "url": expected,
            "secret_token": telegram_webhook_secret_for_org(org.id),
            # Clear stuck queue (e.g. after poller/outage) so last_error is not sticky.
            "drop_pending_updates": "true",
        }
        await _telegram_api_get(token, "setWebhook", **params)
        me = await _telegram_api_get(token, "getMe")
        username = me.get("username")
        bot_username = f"@{username}" if username else None
        webhook_info = await _telegram_api_get(token, "getWebhookInfo")
        webhook_url = (webhook_info.get("url") or "").strip() or None
        if webhook_url and webhook_url.rstrip("/") == expected.rstrip("/"):
            message = f"Webhook зарегистрирован: {bot_username or 'bot'}."
        else:
            message = f"setWebhook выполнен; текущий URL: {webhook_url or 'не настроен'}."
        return _telegram_test_result_from_webhook(
            token_set=True,
            bot_username=bot_username,
            webhook_url=webhook_url,
            expected=expected,
            ok=True,
            message=message,
        )
    except Exception as exc:
        logger.warning(
            "Telegram register webhook failed",
            extra={
                "extra_data": {
                    "event": "org_integrations_register_telegram_webhook_failed",
                    "org_id": org.id,
                    "error": str(exc)[:200],
                }
            },
        )
        return _telegram_test_result_from_webhook(
            token_set=True,
            bot_username=None,
            webhook_url=None,
            expected=expected,
            ok=False,
            message=str(exc)[:300],
        )


def ingress_telegram_bot_token() -> str:
    return (settings.telegram_token or "").strip()


def telegram_webhook_secret_for_org(org_id: int) -> str:
    """Stable per-org secret for Telegram setWebhook (multi-tenant single URL)."""
    pepper = (
        (settings.tenant_secrets_master_key or "").strip()
        or (settings.user_auth_secret or "").strip()
        or "optibot-tg-webhook"
    )
    digest = hash_secret(f"tg-ingress:{org_id}:{pepper}") or ""
    return f"org{org_id}-{digest[:24]}"


async def resolve_org_by_telegram_webhook_secret(secret: str | None) -> Organization | None:
    received = (secret or "").strip()
    if not received:
        return None
    platform = (settings.telegram_webhook_secret or "").strip()
    if platform and hmac.compare_digest(platform, received):
        return await resolve_org_by_telegram_bot_token(ingress_telegram_bot_token())
    async with AsyncSessionLocal() as session:
        orgs = (await session.execute(select(Organization).order_by(Organization.id.asc()))).scalars().all()
    for org in orgs:
        if hmac.compare_digest(telegram_webhook_secret_for_org(org.id), received):
            return org
    return None


async def resolve_org_for_telegram_webhook(secret: str | None) -> Organization | None:
    received = (secret or "").strip()
    platform = (settings.telegram_webhook_secret or "").strip()
    if received:
        return await resolve_org_by_telegram_webhook_secret(received)
    if platform:
        return None
    return await resolve_org_by_telegram_bot_token(ingress_telegram_bot_token())


async def resolve_org_by_telegram_bot_token(bot_token: str) -> Organization | None:
    token = (bot_token or "").strip()
    if not token:
        logger.warning(
            "Telegram ingress: no bot token configured",
            extra={"extra_data": {"event": "tg_resolve_no_token"}},
        )
        return None

    token_hash = hash_secret(token)
    async with AsyncSessionLocal() as session:
        if token_hash:
            stmt = select(Organization).where(Organization.telegram_bot_token_hash == token_hash)
            rows = (await session.execute(stmt)).scalars().all()
        else:
            rows = []
        if not rows:
            stmt = select(Organization).where(Organization.telegram_bot_token.is_not(None))
            candidates = (await session.execute(stmt)).scalars().all()
            rows = [org for org in candidates if get_org_secret(org, "telegram_bot_token") == token]

    if not rows:
        logger.warning(
            "Telegram ingress: unknown bot token",
            extra={"extra_data": {"event": "tg_resolve_unknown_bot"}},
        )
        return None
    if len(rows) > 1:
        logger.warning(
            "Telegram ingress: ambiguous bot token mapping",
            extra={"extra_data": {"event": "tg_resolve_ambiguous_bot", "match_count": len(rows)}},
        )
        return None
    return rows[0]


def telegram_bot_token_for_send(org: Organization) -> str:
    org_token = get_org_secret(org, "telegram_bot_token") or ""
    return resolve_tenant_outbound_secret(org_token, settings.telegram_token)


def telegram_send_url(org: Organization) -> str | None:
    token = telegram_bot_token_for_send(org)
    if not token:
        return None
    return f"https://api.telegram.org/bot{token}/sendMessage"


def _parse_telegram_chat_id(chat_id: int | str) -> int | None:
    if isinstance(chat_id, int):
        return chat_id
    raw = str(chat_id).strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


async def send_telegram_for_org(org: Organization, chat_id: int | str, text: str) -> OutboundSendResult:
    url = telegram_send_url(org)
    if not url:
        logger.warning(
            "Telegram send skipped: token missing",
            extra={
                "extra_data": {
                    "event": "tg_send_skipped_no_token",
                    "org_id": org.id,
                    "chat_id": str(chat_id),
                }
            },
        )
        return OutboundSendResult.skipped("telegram")

    parsed_chat_id = _parse_telegram_chat_id(chat_id)
    if parsed_chat_id is None:
        logger.warning(
            "Telegram send skipped: invalid chat_id",
            extra={
                "extra_data": {
                    "event": "tg_send_invalid_chat_id",
                    "org_id": org.id,
                    "chat_id": str(chat_id),
                }
            },
        )
        return OutboundSendResult.retryable_error("telegram")

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            url, json={"chat_id": parsed_chat_id, "text": text}
        )
    body = getattr(response, "text", "") or ""
    logger.info(
        "Telegram send completed",
        extra={
            "extra_data": {
                "event": "tg_send",
                "org_id": org.id,
                "status_code": response.status_code,
                "chat_id": str(chat_id),
            }
        },
    )
    if response.is_success:
        return OutboundSendResult.success("telegram")
    if telegram_response_auth_failed(response.status_code, body):
        return OutboundSendResult.auth_error("telegram")
    return OutboundSendResult.retryable_error("telegram")

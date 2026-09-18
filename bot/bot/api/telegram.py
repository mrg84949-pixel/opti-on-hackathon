from fastapi import APIRouter, Header, HTTPException, Request

from bot.billing_access import frozen_reply_for_org
from bot.config import settings
from bot.db.database import AsyncSessionLocal
from bot.db.models import Organization
from bot.llm.llm_engine import get_ai_response
from bot.logging_config import get_logger
from bot.services.bot_branding import resolve_bot_welcome_message
from bot.services.telegram_org_service import (
    resolve_org_for_telegram_webhook,
    send_telegram_for_org,
)
from bot.services.webhook_dedup import (
    CHANNEL_TELEGRAM,
    claim_inbound_event_or_duplicate,
    extract_telegram_event_key,
)

logger = get_logger(__name__)

router = APIRouter()


def _webhook_secret_configured() -> str:
    return (settings.telegram_webhook_secret or "").strip()


async def send_telegram_message(org: Organization, chat_id: int, text: str) -> bool:
    result = await send_telegram_for_org(org, chat_id, text)
    return result.ok


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    try:
        data = await request.json()

        if "message" in data and "text" in data["message"]:
            platform_secret = _webhook_secret_configured()
            received_secret = (x_telegram_bot_api_secret_token or "").strip()
            if platform_secret and not received_secret:
                raise HTTPException(status_code=403, detail="Invalid webhook secret")
            org = await resolve_org_for_telegram_webhook(x_telegram_bot_api_secret_token)
            if org is None:
                if platform_secret or received_secret:
                    raise HTTPException(status_code=403, detail="Invalid webhook secret")
                logger.error(
                    "Telegram webhook: organization not resolved",
                    extra={"extra_data": {"event": "tg_webhook_no_org"}},
                )
                return {"status": "error", "detail": "org_not_found"}

            event_key = extract_telegram_event_key(data)
            async with AsyncSessionLocal() as session:
                if not await claim_inbound_event_or_duplicate(
                    session, channel=CHANNEL_TELEGRAM, event_key=event_key
                ):
                    return {"status": "duplicate"}
                await session.commit()

            chat_id = data["message"]["chat"]["id"]
            user_text = data["message"]["text"]
            logger.info(
                "Telegram incoming text",
                extra={
                    "extra_data": {
                        "event": "telegram_webhook_text",
                        "chat_id": str(chat_id),
                        "org_id": org.id,
                        "text_len": len(user_text),
                    }
                },
            )
            if user_text.strip().startswith("/start"):
                start_reply = frozen_reply_for_org(org) or resolve_bot_welcome_message(org)
                sent = await send_telegram_message(org, chat_id, start_reply)
                if not sent:
                    logger.warning(
                        "Telegram /start reply send failed",
                        extra={"extra_data": {"event": "telegram_start_failed", "chat_id": str(chat_id)}},
                    )
                return {"status": "ok"}
            ai_reply = await get_ai_response(
                user_id=str(chat_id),
                user_text=user_text,
                db_memory={},
                channel="telegram",
                org_id=org.id,
            )
            sent = await send_telegram_message(org, chat_id, ai_reply)
            if not sent:
                logger.warning(
                    "Telegram reply send failed",
                    extra={"extra_data": {"event": "telegram_reply_failed", "chat_id": str(chat_id)}},
                )
        else:
            logger.info("Telegram non-text payload ignored", extra={"extra_data": {"event": "telegram_ignored"}})

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            "Telegram webhook error",
            extra={"extra_data": {"event": "telegram_webhook_error", "error_type": type(e).__name__}},
        )
    return {"status": "ok"}

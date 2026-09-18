"""Deterministic yes/no handling while client_change_deadline is active."""
from __future__ import annotations

from datetime import datetime, timezone

from bot.db.database import AsyncSessionLocal
from bot.channels.phone import channel_phone
from bot.llm.tools import get_or_create_customer_for_channel
from bot.logging_config import get_logger
from bot.services import appointment_service, notification_service

logger = get_logger(__name__)


_YES_PHRASES = frozenset(
    {
        "да",
        "ок",
        "ok",
        "yes",
        "согласен",
        "согласна",
        "устраивает",
        "подтверждаю",
        "+",
    }
)
_NO_PHRASES = frozenset(
    {
        "нет",
        "no",
        "отмена",
        "отменить",
        "не устраивает",
        "не согласен",
        "не согласна",
        "-",
    }
)


def parse_client_change_intent(text: str) -> bool | None:
    normalized = (text or "").strip().lower().rstrip(".!?,")
    if not normalized or len(normalized) > 64:
        return None
    if normalized in _YES_PHRASES:
        return True
    if normalized in _NO_PHRASES:
        return False
    return None


async def try_handle_client_change_reply(
    *,
    org_id: int,
    channel: str,
    user_id: str,
    user_text: str,
) -> str | None:
    """Return reply text if handled; None to continue to LLM."""
    if (user_id or "").startswith("admin-sandbox-"):
        return None
    accepted = parse_client_change_intent(user_text)
    if accepted is None:
        return None

    phone = channel_phone(channel, user_id)
    async with AsyncSessionLocal() as session:
        from bot.db.models import Organization

        org = await session.get(Organization, org_id)
        if org is None:
            return None
        customer = await get_or_create_customer_for_channel(
            session, org_id=org_id, channel_phone=phone
        )
        found = await appointment_service.get_appointment_awaiting_client_change(
            session, org_id, customer.id
        )
        if found is None:
            return None
        appt, _cust = found
        tz_name = org.timezone or "UTC"
        try:
            if accepted:
                item, returned_customer = await appointment_service.accept_appointment_change(
                    session, org_id, customer.id
                )
                await session.commit()
                scheduled = datetime.fromisoformat(item["scheduled_at"].replace("Z", "+00:00"))
                msg = notification_service.render_client_change_accepted_text(
                    scheduled, tz_name
                )
                await notification_service.send_customer_message(org, returned_customer, msg)
                return (
                    "Изменение принято. Администратор подтвердит запись. "
                    "Можете задать другие вопросы в чате."
                )
            item, returned_customer = await appointment_service.reject_appointment_change(
                session, org_id, customer.id
            )
            await session.commit()
            scheduled = datetime.fromisoformat(item["scheduled_at"].replace("Z", "+00:00"))
            msg = notification_service.render_cancel_text(
                scheduled, item.get("cancel_reason") or appointment_service.CLIENT_CHANGE_REJECT_REASON
            )
            await notification_service.send_customer_message(org, returned_customer, msg)
            return "Запись отменена по вашему отказу. Чтобы записаться снова, напишите в чат."
        except appointment_service.InvalidStatusTransitionError:
            return (
                "Срок ответа на изменение записи истёк. "
                "Напишите администратору или оформите новую запись."
            )
        except Exception:
            logger.exception(
                "client_change_intent failed",
                extra={
                    "extra_data": {
                        "event": "client_change_intent_error",
                        "org_id": org_id,
                        "customer_id": customer.id,
                        "accepted": accepted,
                    }
                },
            )
            return None

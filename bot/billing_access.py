from __future__ import annotations

from datetime import datetime, timezone

from bot.db.models import Organization

TARIFF_BLOCKED_MESSAGE = (
    "Сервис временно недоступен: период оплаты тарифа истёк. "
    "Свяжитесь с администратором вашей организации или со службой поддержки Optibot."
)

BOT_PAUSED_MESSAGE = (
    "Бот временно отключён администратором. "
    "Запись и консультации сейчас недоступны — попробуйте позже или свяжитесь с клиникой напрямую."
)


def org_subscription_active(org: Organization, *, now: datetime | None = None) -> bool:
    """Если billing_paid_until задан и уже в прошлом — бот отключён (неуплата)."""
    now = now or datetime.now(timezone.utc)
    until = getattr(org, "billing_paid_until", None)
    if until is None:
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until >= now


def org_bot_enabled(org: Organization) -> bool:
    val = getattr(org, "bot_enabled", None)
    if val is None:
        return True
    return bool(val)


def org_bot_operational(org: Organization, *, now: datetime | None = None) -> bool:
    return org_bot_enabled(org) and org_subscription_active(org, now=now)


def frozen_reply_for_org(org: Organization, *, now: datetime | None = None) -> str | None:
    if not org_subscription_active(org, now=now):
        return TARIFF_BLOCKED_MESSAGE
    if not org_bot_enabled(org):
        return BOT_PAUSED_MESSAGE
    return None

"""Customer notifications via Telegram and WhatsApp."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bot.channels.whatsapp import resolve_whatsapp_provider, send_whatsapp_template, send_whatsapp_text
from bot.db.models import Customer, Organization
from bot.services.outbound_health import note_outbound_send_result
from bot.services.outbound_result import OutboundSendResult
from bot.services.telegram_org_service import send_telegram_for_org, telegram_send_url


def telegram_url(org: Organization) -> str | None:
    return telegram_send_url(org)


async def send_telegram(org: Organization, chat_id: str, text: str) -> OutboundSendResult:
    return await send_telegram_for_org(org, chat_id, text)


def wa_digits_from_phone_field(phone: str) -> str:
    p = (phone or "").strip()
    if p.startswith("wa:"):
        p = p.removeprefix("wa:")
    return "".join(c for c in p if c.isdigit())


async def send_whatsapp_customer_message(org: Organization, chat_id: str, plain_text: str) -> OutboundSendResult:
    """Meta: template when configured; otherwise plain text (Green-API or Meta 24h window)."""
    if resolve_whatsapp_provider(org) == "meta":
        tmpl = (org.whatsapp_meta_reminder_template_name or "").strip()
        lang = (org.whatsapp_meta_reminder_template_lang or "ru").strip() or "ru"
        if tmpl:
            digits = wa_digits_from_phone_field(f"wa:{chat_id}" if "@" in chat_id else chat_id)
            if not digits:
                digits = "".join(c for c in chat_id if c.isdigit())
            return await send_whatsapp_template(
                org,
                digits,
                template_name=tmpl,
                language_code=lang,
                body_parameters=[plain_text],
            )
    return await send_whatsapp_text(org, chat_id, plain_text)


async def send_customer_message(org: Organization, customer: Customer, text: str) -> OutboundSendResult:
    phone = (customer.phone or "").strip()
    if phone.startswith("tg:"):
        result = await send_telegram(org, phone.removeprefix("tg:"), text)
    elif phone.startswith("wa:"):
        chat_id = phone.removeprefix("wa:")
        result = await send_whatsapp_customer_message(org, chat_id, text)
    else:
        result = OutboundSendResult.skipped(None)
    note_outbound_send_result(org.id, result)
    return result


def _format_scheduled_at(scheduled_at: datetime) -> str:
    return scheduled_at.astimezone(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def render_confirm_text(scheduled_at: datetime) -> str:
    when = _format_scheduled_at(scheduled_at)
    return f"Ваша запись подтверждена.\nДата и время: {when}"


def render_cancel_text(scheduled_at: datetime, reason: str) -> str:
    when = _format_scheduled_at(scheduled_at)
    return f"Запись отменена.\nДата и время: {when}\nПричина: {reason.strip()}"


def render_complete_text(org: Organization, *, care_message: str | None = None) -> str:
    lines = [
        "Спасибо, что воспользовались нашими услугами!",
        "Будем рады видеть вас снова.",
    ]
    url = (getattr(org, "review_2gis_url", None) or "").strip()
    if url:
        lines.append(f"Оставьте отзыв в 2ГИС: {url}")
    else:
        lines.append("Если понравилось — оставьте отзыв.")
    care = (care_message or "").strip()
    if care:
        lines.append("")
        lines.append("Совет по уходу:")
        lines.append(care)
    upsell_raw = (getattr(org, "post_service_upsell_message", None) or "").strip()
    if upsell_raw:
        org_name = (org.name or "").strip() or "нашу клинику"
        upsell = upsell_raw.replace("{org_name}", org_name)
        lines.append("")
        lines.append(upsell)
    return "\n".join(lines)


DEFAULT_RETENTION_TEMPLATE = (
    "Добрый день! Прошло время после вашего визита в {org_name}. "
    "Будем рады снова записать вас — напишите, если нужна помощь."
)


def render_retention_text(org: Organization, customer: Customer) -> str:
    custom = (getattr(org, "retention_message", None) or "").strip()
    if custom:
        org_name = (org.name or "").strip() or "нашу клинику"
        return custom.replace("{org_name}", org_name)
    org_name = (org.name or "").strip() or "нашу клинику"
    return DEFAULT_RETENTION_TEMPLATE.format(org_name=org_name)


REMINDER_2H_TEMPLATE = (
    "Напоминание{org_suffix}: у вас запись через 2 часа.\n"
    "Дата и время: {when}\n"
    "Чтобы перенести визит, ответьте в чат."
)

REMINDER_24H_TEMPLATE = (
    "Напоминание{org_suffix}: у вас запись через 24 часа.\n"
    "Дата и время: {when}\n"
    "Чтобы перенести визит, ответьте в чат."
)


def _reminder_org_suffix(org_name: str | None) -> str:
    name = (org_name or "").strip()
    return f" от «{name}»" if name else ""


def render_2h_reminder_text(scheduled_at: datetime, *, org_name: str | None = None) -> str:
    when = _format_scheduled_at(scheduled_at)
    return REMINDER_2H_TEMPLATE.format(when=when, org_suffix=_reminder_org_suffix(org_name))


def render_24h_reminder_text(scheduled_at: datetime, *, org_name: str | None = None) -> str:
    when = _format_scheduled_at(scheduled_at)
    return REMINDER_24H_TEMPLATE.format(when=when, org_suffix=_reminder_org_suffix(org_name))


def render_client_cancel_text(scheduled_at: datetime, reason: str) -> str:
    when = _format_scheduled_at(scheduled_at)
    reason_line = f"\nПричина: {reason.strip()}" if (reason or "").strip() else ""
    return (
        f"Ваша запись отменена.\n"
        f"Дата и время (отменённый визит): {when}{reason_line}\n"
        "Чтобы записаться снова, напишите в чат."
    )


def render_reschedule_text(scheduled_at: datetime, tz_name: str) -> str:
    try:
        when = scheduled_at.astimezone(ZoneInfo(tz_name or "UTC")).strftime("%d.%m.%Y %H:%M")
    except Exception:
        when = _format_scheduled_at(scheduled_at)
    return (
        f"Запись перенесена на {when} ({tz_name}).\n"
        "Администратор подтвердит новое время и пришлёт уведомление."
    )


def render_crm_reschedule_text(scheduled_at: datetime, tz_name: str) -> str:
    """For a reschedule the CLINIC made directly in the CRM (not a client
    request via the bot) — no "admin will confirm" framing, since staff
    already acted on their own system."""
    try:
        when = scheduled_at.astimezone(ZoneInfo(tz_name or "UTC")).strftime("%d.%m.%Y %H:%M")
    except Exception:
        when = _format_scheduled_at(scheduled_at)
    return f"Время вашей записи изменено администратором.\nНовое время: {when}"


def _format_local_when(scheduled_at: datetime, tz_name: str) -> str:
    try:
        return scheduled_at.astimezone(ZoneInfo(tz_name or "UTC")).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return _format_scheduled_at(scheduled_at)


def render_admin_change_proposal_text(
    scheduled_at: datetime,
    reason: str,
    *,
    tz_name: str = "UTC",
    response_hours: int = 24,
) -> str:
    when = _format_local_when(scheduled_at, tz_name)
    hours = max(1, int(response_hours))
    return (
        "Администратор изменил вашу запись.\n"
        f"Новая дата и время: {when}\n"
        f"Причина: {reason.strip()}\n\n"
        f"Устраивает ли вам новое время? Ответьте в течение {hours} ч.: "
        "«да» — согласен, «нет» — отменить запись."
    )


def render_client_change_accepted_text(scheduled_at: datetime, tz_name: str = "UTC") -> str:
    when = _format_local_when(scheduled_at, tz_name)
    return (
        f"Спасибо! Вы приняли изменение записи на {when}.\n"
        "Администратор подтвердит запись и пришлёт уведомление."
    )


def render_client_change_timeout_text(scheduled_at: datetime, tz_name: str = "UTC") -> str:
    when = _format_local_when(scheduled_at, tz_name)
    return (
        f"Запись на {when} отменена: мы не получили ваш ответ в течение 24 часов.\n"
        "Чтобы записаться снова, напишите в чат."
    )

from __future__ import annotations

from bot.db.models import Organization
from bot.llm.booking_draft import build_booking_clock_context
from bot.llm.prompts import PLATFORM_GUARDRAILS

DEFAULT_BOT_DISPLAY_NAME = "ассистент"
DEFAULT_BOT_WELCOME = (
    "Здравствуйте! Я {name}, помогу записаться на услугу или ответить на вопросы. "
    "Напишите, чем могу помочь."
)
ALLOWED_BOT_TONES = frozenset({"formal", "friendly"})

_TONE_SNIPPETS = {
    "formal": (
        "Стиль общения: официальный и сдержанный. Обращайтесь на «вы», без сленга и эмодзи."
    ),
    "friendly": (
        "Стиль общения: тёплый и дружелюбный, но профессиональный. Можно лёгкая неформальность."
    ),
}


def resolve_bot_display_name(org: Organization) -> str:
    custom = (getattr(org, "bot_display_name", None) or "").strip()
    return custom or DEFAULT_BOT_DISPLAY_NAME


def resolve_bot_welcome_message(org: Organization) -> str:
    custom = (getattr(org, "bot_welcome_message", None) or "").strip()
    if custom:
        return custom
    name = resolve_bot_display_name(org)
    return DEFAULT_BOT_WELCOME.format(name=name)


def tone_system_snippet(tone: str | None) -> str:
    key = (tone or "").strip().lower()
    if not key:
        return ""
    return _TONE_SNIPPETS.get(key, "")


def build_org_system_instruction(
    org: Organization,
    *,
    base: str,
    manage_addon: str = "",
    context_summary: str = "",
) -> str:
    parts = [PLATFORM_GUARDRAILS.strip(), base.strip()]
    parts.append(build_booking_clock_context(getattr(org, "timezone", None) or "UTC"))
    display_name = resolve_bot_display_name(org)
    parts.append(f"Тебя зовут {display_name}. Представляйся клиенту этим именем при необходимости.")
    tone_block = tone_system_snippet(getattr(org, "bot_tone", None))
    if tone_block:
        parts.append(tone_block)
    if manage_addon.strip():
        parts.append(manage_addon.strip())
    if context_summary.strip():
        parts.append("Краткий контекст клиента (из БД):\n" + context_summary.strip())
    extra_prompt = (org.system_prompt or "").strip()
    if extra_prompt:
        parts.append(
            "Дополнительные инструкции организации (не должны противоречить платформенным правилам выше):\n"
            + extra_prompt
        )
    return "\n\n".join(parts)

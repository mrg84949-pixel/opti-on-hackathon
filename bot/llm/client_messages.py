"""Client-safe reply templates and leak filter for bot ingress."""

from __future__ import annotations

import re

AI_UNAVAILABLE = (
    "Ассистент временно недоступен. "
    "Попробуйте через несколько минут или напишите администратору."
)
AI_RATE_LIMIT = (
    "Сейчас высокая нагрузка на ассистента. "
    "Попробуйте через несколько минут."
)
AI_CONFIG_ERROR = (
    "Сервис записи временно недоступен. "
    "Мы уже работаем над восстановлением."
)
TOOL_LOOP_EXHAUSTED = (
    "Не удалось завершить ответ. Попробуйте переформулировать вопрос."
)

_INTERNAL_LEAK_MARKERS = (
    "groq",
    "gemini",
    "ai_provider",
    "ai_use_stub",
    "api_key",
    ".env",
    "default_org_id",
    "переключите",
    "llama-",
    "openai",
)

# Raw LLM tool instructions that must not reach the client (KI-02 / T07).
ASSISTANT_LEAK_MARKERS = (
    "сообщите клиенту",
    "cancel_appointment",
    "edit_appointment",
    "set_customer_name",
    "get_available_slots",
    "create_appointment",
    "контекст сжат",
    "статус «новая»",
    "<function",
    "</function>",
)

_CONFIRM_LEAK_RE = re.compile(
    r"запись создана \(номер \d+\):\s*(.+?)\s*\(([^)]+)\),\s*статус",
    re.IGNORECASE,
)
_CANCEL_LEAK_RE = re.compile(r"запись №(\d+) отменена", re.IGNORECASE)
_FUNCTION_MARKUP_ANY_RE = re.compile(
    r"<function\s*=.*?</function>",
    re.DOTALL | re.IGNORECASE,
)
_FUNCTION_MARKUP_NO_GT_RE = re.compile(
    r"<function=[a-zA-Z0-9_]+\s*\{.*?</function>",
    re.DOTALL | re.IGNORECASE,
)


def strip_assistant_instructions(text: str) -> str:
    """Drop bracketed tool instructions before client delivery."""
    normalized = (text or "").strip()
    if "[Инструкция" in normalized:
        return normalized.split("[Инструкция", 1)[0].strip()
    return normalized


def render_handoff_client_text() -> str:
    """Client-facing text after human handoff (FSM / direct paths)."""
    return (
        "Запрос передан администратору. С вами свяжутся в ближайшее время."
    )


def _sanitize_confirm_leak(text: str) -> str | None:
    match = _CONFIRM_LEAK_RE.search(text)
    if match is None:
        return None
    when_label = match.group(1).strip()
    timezone_name = match.group(2).strip()
    auto_confirmed = "статус «подтверждена»" in text.lower()
    return render_booking_created_client_text(
        when_label=when_label,
        timezone_name=timezone_name,
        service_name="",
        auto_confirmed=auto_confirmed,
    )


def _sanitize_cancel_leak(text: str) -> str | None:
    match = _CANCEL_LEAK_RE.search(text)
    if match is None:
        return None
    return f"Запись №{match.group(1)} отменена."


def sanitize_client_reply(text: str) -> str:
    """Last-line filter before any client ingress reply."""
    normalized = strip_assistant_instructions(text)
    if not normalized:
        return AI_UNAVAILABLE
    # Strip Groq-style tool markup; if anything remains that looks like a tool call, block.
    stripped = _FUNCTION_MARKUP_ANY_RE.sub("", normalized)
    stripped = _FUNCTION_MARKUP_NO_GT_RE.sub("", stripped).strip()
    if "<function" in stripped.lower() or "</function>" in stripped.lower():
        return AI_UNAVAILABLE
    normalized = stripped or normalized
    lowered = normalized.lower()
    if any(marker in lowered for marker in _INTERNAL_LEAK_MARKERS):
        return AI_UNAVAILABLE
    if any(marker in lowered for marker in ASSISTANT_LEAK_MARKERS):
        if "запрос передан администратору" in lowered:
            return render_handoff_client_text()
        confirm_safe = _sanitize_confirm_leak(normalized)
        if confirm_safe is not None:
            return confirm_safe
        cancel_safe = _sanitize_cancel_leak(normalized)
        if cancel_safe is not None:
            return cancel_safe
        return AI_UNAVAILABLE
    # Pure tool-markup-only replies become empty after strip → fallback.
    if not normalized.strip():
        return AI_UNAVAILABLE
    return normalized


def render_booking_created_client_text(
    *,
    when_label: str,
    timezone_name: str,
    service_name: str,
    auto_confirmed: bool,
) -> str:
    """Client-facing text after deterministic confirm (FSM / shortcuts)."""
    service = (service_name or "").strip()
    service_line = f"\nУслуга: {service}." if service else ""
    tz = (timezone_name or "UTC").strip()
    when = (when_label or "").strip()
    if auto_confirmed:
        return (
            f"Запись подтверждена на {when} ({tz}).{service_line}\n"
            "Ждём вас!"
        )
    return (
        f"Запись оформлена на {when} ({tz}).{service_line}\n"
        "Администратор подтвердит время и напишет вам."
    )

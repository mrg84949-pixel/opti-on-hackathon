"""Compressed dialog context stored on Customer.dialog_context."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

CONTEXT_SUMMARY_KEY = "context_summary"
CONTEXT_COMPRESSED_AT_KEY = "compressed_at"
MIN_SUMMARY_LEN = 20
MAX_SUMMARY_LEN = 4000


def get_context_summary(dialog_context: dict[str, Any] | None) -> str | None:
    if not dialog_context:
        return None
    text = (dialog_context.get(CONTEXT_SUMMARY_KEY) or "").strip()
    return text or None


def validate_summary_text(summary: str) -> str:
    trimmed = (summary or "").strip()
    if len(trimmed) < MIN_SUMMARY_LEN:
        raise ValueError(
            f"Слишком короткое summary (минимум {MIN_SUMMARY_LEN} символов). "
            "Добавь имя, услугу, дату записи и статус."
        )
    if len(trimmed) > MAX_SUMMARY_LEN:
        trimmed = trimmed[:MAX_SUMMARY_LEN]
    return trimmed


def apply_summary_to_context(
    dialog_context: dict[str, Any] | None, summary: str
) -> dict[str, Any]:
    ctx = dict(dialog_context or {})
    ctx[CONTEXT_SUMMARY_KEY] = validate_summary_text(summary)
    ctx[CONTEXT_COMPRESSED_AT_KEY] = datetime.now(timezone.utc).isoformat()
    return ctx


def build_post_complete_summary(
    *,
    appointment_id: int,
    when_label: str,
    timezone_name: str = "UTC",
) -> str:
    return (
        f"Услуга завершена {when_label} ({timezone_name}). "
        f"Запись №{appointment_id}. Клиент может записаться снова."
    )


def build_post_booking_summary(
    *,
    customer_name: str,
    service: str,
    when_label: str,
    appointment_id: int,
    timezone_name: str,
) -> str:
    name = (customer_name or "Клиент").strip()
    svc = (service or "услуга").strip()
    return (
        f"Клиент {name} записан на «{svc}» {when_label} ({timezone_name}). "
        f"Запись №{appointment_id}, статус «новая», ждёт подтверждения администратора."
    )

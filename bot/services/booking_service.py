"""Booking confirm orchestration shared by LLM tools and deterministic shortcuts."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass

from bot.db.database import AsyncSessionLocal
from bot.db.models import Organization
from bot.llm.booking_draft import get_pending_booking, validate_draft_schedule
from bot.llm.context_summary import build_post_booking_summary

# Per-customer lock so two near-simultaneous confirmations (webhook redelivery
# racing a second "да", or a user double-tapping) can't both read the same
# pending draft and both call the CRM's book_appointment. Single-process
# assumption: main.py/docker-compose.prod.yml run uvicorn with no --workers,
# so this is a real cross-request guard in the current deployment — it would
# NOT protect across multiple worker processes if that ever changes.
_confirm_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


@dataclass(frozen=True)
class ConfirmBookingResult:
    appointment_id: int = 0
    when_label: str = ""
    timezone_name: str = ""
    service_name: str = ""
    auto_confirmed: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def render_confirm_assistant_text(result: ConfirmBookingResult) -> str:
    """LLM tool return — instructions for the assistant, not for the client."""
    if result.error:
        return result.error
    if result.auto_confirmed:
        return (
            f"Запись создана (номер {result.appointment_id}): {result.when_label} "
            f"({result.timezone_name}), статус «подтверждена». Клиенту отправлено подтверждение. "
            "Контекст сжат в БД. Для отмены или переноса — cancel_appointment / edit_appointment."
        )
    return (
        f"Запись создана (номер {result.appointment_id}): {result.when_label} "
        f"({result.timezone_name}), статус «новая». Сообщите клиенту, что администратор подтвердит время. "
        "Контекст сжат в БД. Для отмены или переноса — cancel_appointment / edit_appointment."
    )


async def confirm_pending_booking(*, org_id: int, customer_id: int) -> ConfirmBookingResult:
    """Create appointment from pending draft after client confirmed the card.

    Serialized per customer_id via _confirm_locks, with a single commit at the
    end of the critical section, so two concurrent confirmations for the same
    customer can't both read the same pending draft and both call the CRM's
    book_appointment. See docs/macdent-api-reference.md audit notes.
    """
    from bot.llm import tools as llm_tools

    async with _confirm_locks[customer_id]:
        async with AsyncSessionLocal() as session:
            customer = await llm_tools._load_customer(session, customer_id)
            if customer is None:
                return ConfirmBookingResult(error="Ошибка: профиль клиента не найден.")
            org = await session.get(Organization, org_id)
            if org is None:
                return ConfirmBookingResult(error="Ошибка: организация не найдена.")

            draft = get_pending_booking(customer.dialog_context)
            if draft is None:
                return ConfirmBookingResult(
                    error=(
                        "Нет черновика записи. Сначала собери данные и вызови show_appointment_card, "
                        "дождись подтверждения клиента."
                    )
                )
            errors = validate_draft_schedule(draft, org.timezone or "UTC")
            if errors:
                return ConfirmBookingResult(
                    error="Черновик неполный или дата недопустима: " + ", ".join(errors)
                )

            slot_err = await llm_tools._validate_draft_slot(session, org, draft)
            if slot_err:
                return ConfirmBookingResult(error=slot_err)

            await llm_tools._persist_draft(session, customer, draft, commit=False)

            try:
                appt, local_dt, auto_confirmed = await llm_tools._create_appointment_from_draft(
                    session, org=org, customer=customer, draft=draft
                )
            except ValueError as exc:
                return ConfirmBookingResult(error=str(exc))
            except Exception as exc:
                return ConfirmBookingResult(error=f"Ошибка при создании записи: {exc}")

            tz_name = org.timezone or "UTC"
            when_label = local_dt.strftime("%d.%m.%Y %H:%M")
            auto_summary = build_post_booking_summary(
                customer_name=draft.get("customer_name") or customer.name or "",
                service=draft.get("service") or "",
                when_label=when_label,
                appointment_id=appt.id,
                timezone_name=tz_name,
            )
            await llm_tools._set_manage_after_booking(
                session, customer, appt.id, context_summary=auto_summary, commit=False
            )
            await session.commit()
            return ConfirmBookingResult(
                appointment_id=appt.id,
                when_label=when_label,
                timezone_name=tz_name,
                service_name=(draft.get("service") or "").strip(),
                auto_confirmed=auto_confirmed,
            )

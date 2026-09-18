"""Linear booking FSM without LLM (name → service → staff → date → time → confirm)."""
from __future__ import annotations

import re
from typing import TypeAlias

from bot.channels.phone import channel_phone
from bot.config import settings
from bot.db.database import AsyncSessionLocal
from bot.llm.booking_draft import (
    PENDING_BOOKING_KEY,
    get_pending_booking,
    merge_draft,
    normalize_booking_date,
    normalize_booking_time,
    render_appointment_card,
)
from bot.llm.context import TurnContext
from bot.llm.staff_choice import (
    AWAITING_STAFF_CHOICE_KEY,
    format_staff_choice_for_client,
    normalize_staff_items,
)
from bot.llm.tools import (
    DIALOG_MODE_MANAGE,
    get_or_create_customer_for_channel,
    load_services_catalog_for_org,
    resolve_tool_mode,
    tool_by_name,
)
from bot.llm.client_messages import render_handoff_client_text
from bot.services import crm_staff_service, handoff_service

BOOKING_STEP_KEY = "booking_step"
STEPS = ("name", "service", "staff", "date", "time", "confirm")

FsmResult: TypeAlias = tuple[str, str]

_BOOKING_ENTRY_RE = re.compile(
    r"(запис|хочу\s+запис|запишите|нужна\s+запись|оформить\s+запись)",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(r"^(отмена|отменить|стоп)$", re.IGNORECASE)
_HUMAN_RE = re.compile(r"(человек|администрат|оператор|живой)", re.IGNORECASE)

_TOOL_ERROR_PREFIXES = ("Ошибка", "Не удалось", "Не хватает", "Нельзя", "Черновик")


def matches_booking_entry_intent(user_text: str) -> bool:
    raw = (user_text or "").strip()
    if not raw or len(raw) > 128:
        return False
    return _BOOKING_ENTRY_RE.search(raw) is not None


def _parse_catalog_service_names(catalog_text: str) -> list[str]:
    names: list[str] = []
    for line in (catalog_text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        rest = stripped[2:].strip()
        if not rest:
            continue
        name = rest.split(":", 1)[0].strip()
        if name:
            names.append(name)
    return names


def match_service_from_catalog(user_text: str, catalog_text: str) -> str | None:
    raw = (user_text or "").strip().lower()
    if not raw:
        return None
    names = _parse_catalog_service_names(catalog_text)
    matches = [name for name in names if raw in name.lower() or name.lower() in raw]
    if len(matches) == 1:
        return matches[0]
    return None


def _is_sandbox_user(user_id: str) -> bool:
    return (user_id or "").startswith("admin-sandbox-")


def _is_tool_error(result: str) -> bool:
    text = (result or "").strip()
    return any(text.startswith(prefix) for prefix in _TOOL_ERROR_PREFIXES)


def _strip_llm_instructions(text: str) -> str:
    if not text:
        return ""
    if "[Инструкция" in text:
        return text.split("[Инструкция", 1)[0].strip()
    return text.strip()


def _slots_reply_for_client(tool_result: str) -> str:
    text = _strip_llm_instructions(tool_result)
    if "Свободные окна на" in text:
        text = text.replace(
            "Предложи клиенту выбрать время.",
            "Выберите удобное время.",
        )
    return text


async def _load_active_staff(session, org_id: int) -> list[dict]:
    payload = await crm_staff_service.list_crm_staff(session, org_id)
    return [row for row in payload.get("items") or [] if row.get("active", True)]


async def _build_turn_context(session, org_id: int, customer_id: int) -> TurnContext:
    catalog = await load_services_catalog_for_org(session, org_id)
    return TurnContext(org_id=org_id, customer_id=customer_id, services_catalog=catalog)


def _get_step(ctx_data: dict) -> str | None:
    step = ctx_data.get(BOOKING_STEP_KEY)
    if isinstance(step, str) and step in STEPS:
        return step
    return None


async def _clear_fsm_state(
    session,
    customer,
    *,
    clear_draft: bool = False,
) -> None:
    ctx_data = dict(customer.dialog_context or {})
    ctx_data.pop(BOOKING_STEP_KEY, None)
    ctx_data.pop(AWAITING_STAFF_CHOICE_KEY, None)
    if clear_draft:
        ctx_data.pop(PENDING_BOOKING_KEY, None)
    customer.dialog_context = ctx_data
    await session.commit()


async def _set_step(session, customer, step: str, **extra) -> dict:
    ctx_data = dict(customer.dialog_context or {})
    ctx_data[BOOKING_STEP_KEY] = step
    ctx_data.update(extra)
    customer.dialog_context = ctx_data
    await session.commit()
    return ctx_data


async def _handle_cancel(session, customer, ctx: TurnContext) -> str:
    await tool_by_name(ctx, "cancel_appointment_draft")()
    await _clear_fsm_state(session, customer, clear_draft=True)
    return "Запись отменена. Напишите, если захотите записаться снова."


async def _handle_human(session, customer, ctx: TurnContext) -> str:
    ctx.human_transfer_requested = True
    await handoff_service.request_human_handoff(
        org_id=ctx.org_id,
        customer_id=ctx.customer_id,
    )
    await _clear_fsm_state(session, customer)
    return render_handoff_client_text()


async def _advance_after_service(
    session,
    customer,
    org_id: int,
    ctx: TurnContext,
    service_name: str,
) -> str:
    ctx_data = dict(customer.dialog_context or {})
    draft = merge_draft(get_pending_booking(ctx_data), service=service_name)
    ctx_data[PENDING_BOOKING_KEY] = draft
    staff = await _load_active_staff(session, org_id)
    if len(staff) > 1:
        normalized = normalize_staff_items(staff)
        ctx_data[AWAITING_STAFF_CHOICE_KEY] = normalized
        ctx_data[BOOKING_STEP_KEY] = "staff"
        customer.dialog_context = ctx_data
        await session.commit()
        return (
            f"Услуга: {service_name}.\n\n"
            f"{format_staff_choice_for_client(normalized)}"
        )
    if len(staff) == 1:
        row = normalize_staff_items(staff)[0]
        draft = merge_draft(draft, doctor_id=row["id"], doctor_name=row["name"])
        ctx_data[PENDING_BOOKING_KEY] = draft
    ctx_data[BOOKING_STEP_KEY] = "date"
    customer.dialog_context = ctx_data
    await session.commit()
    return f"Услуга: {service_name}. На какой день удобно записаться?"


async def _handle_step(
    *,
    step: str,
    user_text: str,
    session,
    customer,
    org,
    ctx: TurnContext,
) -> str | None:
    org_id = org.id
    tz_name = org.timezone or "UTC"
    ctx_data = dict(customer.dialog_context or {})

    if step == "name":
        name = (user_text or "").strip()
        if len(name) < 2:
            return "Пожалуйста, напишите имя полностью (минимум 2 символа)."
        result = await tool_by_name(ctx, "set_customer_name")(name)
        if _is_tool_error(result):
            return "Не удалось сохранить имя. Напишите, как к вам обращаться."
        catalog = await load_services_catalog_for_org(session, org_id)
        await _set_step(session, customer, "service")
        return (
            f"Приятно познакомиться, {name}!\n\n"
            "Выберите услугу из каталога:\n"
            f"{catalog}\n\n"
            "Напишите название услуги."
        )

    if step == "service":
        catalog = await load_services_catalog_for_org(session, org_id)
        matched = match_service_from_catalog(user_text, catalog)
        if matched is None:
            return (
                "Не удалось определить услугу. Выберите из списка:\n"
                f"{catalog}\n\n"
                "Напишите точное название услуги."
            )
        return await _advance_after_service(session, customer, org_id, ctx, matched)

    if step == "staff":
        if ctx_data.get(AWAITING_STAFF_CHOICE_KEY):
            return None
        staff = await _load_active_staff(session, org_id)
        normalized = normalize_staff_items(staff)
        if not normalized:
            return "Специалисты пока не настроены. Напишите «человек», чтобы связаться с администратором."
        ctx_data[AWAITING_STAFF_CHOICE_KEY] = normalized
        customer.dialog_context = ctx_data
        await session.commit()
        return format_staff_choice_for_client(normalized)

    if step == "date":
        date_iso = normalize_booking_date(user_text, tz_name)
        if not date_iso:
            return "На какой день удобно? Можно написать «завтра» или конкретную дату."
        draft = merge_draft(get_pending_booking(ctx_data), date=date_iso)
        ctx_data[PENDING_BOOKING_KEY] = draft
        customer.dialog_context = ctx_data
        await session.flush()
        doctor_id = (draft.get("doctor_id") or "").strip()
        slots_result = await tool_by_name(ctx, "get_available_slots")(doctor_id, date_iso)
        if _is_tool_error(slots_result) or "нет" in slots_result.lower():
            return _slots_reply_for_client(slots_result)
        await _set_step(session, customer, "time", **{PENDING_BOOKING_KEY: draft})
        return _slots_reply_for_client(slots_result)

    if step == "time":
        time_hm = normalize_booking_time(user_text)
        if not time_hm:
            return "Уточните время, например «15:00» или «в 15» — из предложенных свободных окон."
        draft = merge_draft(get_pending_booking(ctx_data), time=time_hm)
        card_result = await tool_by_name(ctx, "show_appointment_card")(
            draft.get("customer_name") or customer.name or "",
            draft.get("service") or "",
            draft.get("date") or "",
            time_hm,
            draft.get("doctor_id") or "",
            draft.get("doctor_name") or "",
        )
        if _is_tool_error(card_result) or "Проверьте запись" not in card_result:
            return _strip_llm_instructions(card_result) or (
                "Не удалось оформить карточку. Выберите другое время из свободных окон."
            )
        await session.refresh(customer)
        persisted = get_pending_booking(customer.dialog_context) or draft
        card_text = render_appointment_card(persisted, tz_name=tz_name)
        await _set_step(
            session,
            customer,
            "confirm",
            **{PENDING_BOOKING_KEY: persisted},
        )
        return card_text

    if step == "confirm":
        return None

    return None


async def try_handle_booking_fsm(
    *,
    org_id: int,
    channel: str,
    user_id: str,
    user_text: str,
) -> FsmResult | None:
    """Run linear booking FSM when enabled; None to continue to LLM."""
    if _is_sandbox_user(user_id):
        return None
    if not settings.ai_booking_fsm:
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
        if await resolve_tool_mode(session, org_id, customer) == DIALOG_MODE_MANAGE:
            return None

        ctx = await _build_turn_context(session, org_id, customer.id)
        ctx_data = dict(customer.dialog_context or {})
        step = _get_step(ctx_data)

        if _CANCEL_RE.match((user_text or "").strip()):
            return await _handle_cancel(session, customer, ctx), "booking_fsm"
        if _HUMAN_RE.search((user_text or "").strip()):
            return await _handle_human(session, customer, ctx), "booking_fsm"

        if step is None:
            if not matches_booking_entry_intent(user_text):
                return None
            await _set_step(session, customer, "name")
            return "Здравствуйте! Как вас зовут?", "booking_fsm"

        reply = await _handle_step(
            step=step,
            user_text=user_text,
            session=session,
            customer=customer,
            org=org,
            ctx=ctx,
        )
        if reply is None:
            return None
        return reply, "booking_fsm"

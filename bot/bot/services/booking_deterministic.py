"""Deterministic booking shortcuts before LLM (staff choice, confirm, catalog)."""
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
    validate_draft_schedule,
)
from bot.llm.client_messages import render_booking_created_client_text
from bot.llm.context import TurnContext
from bot.llm.booking_fsm import BOOKING_STEP_KEY
from bot.llm.staff_choice import AWAITING_STAFF_CHOICE_KEY, parse_staff_choice
from bot.llm.tools import (
    DIALOG_MODE_MANAGE,
    get_or_create_customer_for_channel,
    load_services_catalog_for_org,
    resolve_tool_mode,
    tool_by_name,
)
from bot.services import booking_service, crm_staff_service
from bot.services.client_change_intent import parse_client_change_intent

ShortcutResult: TypeAlias = tuple[str, str]

_SERVICES_CATALOG_RE = re.compile(
    r"(какие\s+услуги|прайс|сколько\s+стоит|^услуги$|что\s+у\s+вас\s+есть)",
    re.IGNORECASE,
)

_CONFIRM_TOOL_ERROR_PREFIXES = ("Черновик", "Ошибка", "Не хватает", "Нельзя")


def parse_yes_no(text: str) -> bool | None:
    """Reuse client change yes/no parser for booking confirm shortcut."""
    return parse_client_change_intent(text)


def _is_sandbox_user(user_id: str) -> bool:
    return (user_id or "").startswith("admin-sandbox-")


async def _load_active_staff_count(session, org_id: int) -> int:
    payload = await crm_staff_service.list_crm_staff(session, org_id)
    items = [row for row in payload.get("items") or [] if row.get("active", True)]
    return len(items)


def _matches_services_catalog_intent(user_text: str) -> bool:
    raw = (user_text or "").strip()
    if not raw or len(raw) > 64:
        return False
    return _SERVICES_CATALOG_RE.search(raw) is not None


def _is_confirm_tool_error(result: str) -> bool:
    text = (result or "").strip()
    return any(text.startswith(prefix) for prefix in _CONFIRM_TOOL_ERROR_PREFIXES)


async def try_handle_staff_choice(
    *,
    org_id: int,
    channel: str,
    user_id: str,
    user_text: str,
) -> str | None:
    """Return reply text if staff choice was handled; None to continue to LLM."""
    if _is_sandbox_user(user_id):
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
        ctx_data = dict(customer.dialog_context or {})
        staff_items = ctx_data.get(AWAITING_STAFF_CHOICE_KEY)
        if not isinstance(staff_items, list) or not staff_items:
            return None

        matched = parse_staff_choice(user_text, staff_items)
        if matched is None:
            return None

        draft = merge_draft(
            get_pending_booking(ctx_data),
            doctor_id=matched["id"],
            doctor_name=matched["name"],
        )
        ctx_data[PENDING_BOOKING_KEY] = draft
        ctx_data.pop(AWAITING_STAFF_CHOICE_KEY, None)
        if ctx_data.get(BOOKING_STEP_KEY) == "staff":
            ctx_data[BOOKING_STEP_KEY] = "date"
        customer.dialog_context = ctx_data
        await session.commit()

        name = matched["name"]
        return f"Записываем к {name}. На какой день удобно?"


async def try_handle_confirm_draft(
    *,
    org_id: int,
    channel: str,
    user_id: str,
    user_text: str,
) -> str | None:
    """Confirm or cancel a complete pending booking draft on yes/no."""
    if _is_sandbox_user(user_id):
        return None

    accepted = parse_yes_no(user_text)
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
        if await resolve_tool_mode(session, org_id, customer) == DIALOG_MODE_MANAGE:
            return None

        draft = get_pending_booking(customer.dialog_context)
        if draft is None:
            return None

        tz_name = org.timezone or "UTC"
        if validate_draft_schedule(draft, tz_name):
            return None

        staff_count = await _load_active_staff_count(session, org_id)
        if staff_count > 1 and not (draft.get("doctor_id") or "").strip():
            return None

        catalog = await load_services_catalog_for_org(session, org_id)
        ctx = TurnContext(
            org_id=org_id,
            customer_id=customer.id,
            services_catalog=catalog,
        )

        if not accepted:
            await tool_by_name(ctx, "cancel_appointment_draft")()
            await session.refresh(customer)
            ctx_data = dict(customer.dialog_context or {})
            ctx_data.pop(BOOKING_STEP_KEY, None)
            ctx_data.pop(PENDING_BOOKING_KEY, None)
            customer.dialog_context = ctx_data
            await session.commit()
            return (
                "Черновик отменён. Что изменить — услугу, дату, время или специалиста?"
            )

        result = await booking_service.confirm_pending_booking(
            org_id=org_id,
            customer_id=customer.id,
        )
        if not result.ok:
            if result.error and _is_confirm_tool_error(result.error):
                return None
            return None
        await session.refresh(customer)
        ctx_data = dict(customer.dialog_context or {})
        ctx_data.pop(BOOKING_STEP_KEY, None)
        customer.dialog_context = ctx_data
        await session.commit()
        return render_booking_created_client_text(
            when_label=result.when_label,
            timezone_name=result.timezone_name,
            service_name=result.service_name,
            auto_confirmed=result.auto_confirmed,
        )


async def try_handle_services_catalog(
    *,
    org_id: int,
    channel: str,
    user_id: str,
    user_text: str,
) -> str | None:
    """Return services catalog for common price/catalog phrases."""
    if _is_sandbox_user(user_id):
        return None
    if not _matches_services_catalog_intent(user_text):
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

        catalog = await load_services_catalog_for_org(session, org_id)
        return catalog.strip() if catalog else None


async def try_handle_booking_shortcut(
    *,
    org_id: int,
    channel: str,
    user_id: str,
    user_text: str,
) -> ShortcutResult | None:
    """Run deterministic booking shortcuts before LLM."""
    reply = await try_handle_staff_choice(
        org_id=org_id,
        channel=channel,
        user_id=user_id,
        user_text=user_text,
    )
    if reply is not None:
        return reply, "staff_choice"

    if not settings.ai_booking_shortcuts:
        return None

    reply = await try_handle_confirm_draft(
        org_id=org_id,
        channel=channel,
        user_id=user_id,
        user_text=user_text,
    )
    if reply is not None:
        return reply, "booking_shortcut"

    reply = await try_handle_services_catalog(
        org_id=org_id,
        channel=channel,
        user_id=user_id,
        user_text=user_text,
    )
    if reply is not None:
        return reply, "booking_shortcut"

    return None

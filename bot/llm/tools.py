from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.crm import get_crm_provider
from bot.services.appointment_service import SLOT_TAKEN_MESSAGE
from bot.db.database import AsyncSessionLocal
from bot.db.models import Appointment, AppointmentStatus, Customer, Organization
from bot.llm.booking_draft import (
    PAST_BOOKING_MESSAGE,
    PENDING_BOOKING_KEY,
    TIME_UNPARSEABLE_MESSAGE,
    TIME_VAGUE_MESSAGE,
    build_booking_clock_context,
    get_pending_booking,
    is_vague_booking_time,
    merge_draft,
    normalize_booking_date,
    normalize_booking_time,
    parse_scheduled_local,
    render_appointment_card,
    validate_draft_for_card,
    validate_draft_schedule,
)
from bot.llm.context import TurnContext
from bot.llm.context_summary import (
    apply_summary_to_context,
    build_post_booking_summary,
    get_context_summary,
    validate_summary_text,
)
from bot.llm.prompts import EMPTY_SERVICES_CATALOG
from bot.llm.staff_choice import (
    AWAITING_STAFF_CHOICE_KEY,
    format_staff_choice_for_llm,
    normalize_staff_items,
    resolve_staff_reference,
    staff_choice_prompt_for_llm,
    staff_unknown_choice_message,
)
from bot.logging_config import get_logger
from bot.notifications import (
    notify_admins_about_new_appointment,
)
from bot.services import appointment_service, booking_service, crm_staff_service, customer_service, handoff_service, notification_service
from bot.services.org_services_catalog import resolve_service_price_minor

logger = get_logger(__name__)

DIALOG_MODE_KEY = "dialog_mode"
LAST_APPOINTMENT_ID_KEY = "last_appointment_id"
DIALOG_MODE_BOOKING = "booking"
DIALOG_MODE_MANAGE = "manage"

STAFF_NOT_CONFIGURED_MESSAGE = (
    "[Инструкция: специалисты не настроены. Сообщи клиенту и при необходимости transfer_to_human.]"
)


async def _load_customer(session: AsyncSession, customer_id: int | None) -> Customer | None:
    if customer_id is None:
        return None
    return await session.get(Customer, customer_id)


async def _persist_dialog_context(
    session: AsyncSession, customer: Customer, updates: dict, *, commit: bool = True
) -> None:
    ctx_data = dict(customer.dialog_context or {})
    ctx_data.update(updates)
    customer.dialog_context = ctx_data
    if commit:
        await session.commit()
    else:
        await session.flush()


async def _persist_draft(
    session: AsyncSession, customer: Customer, draft: dict, *, commit: bool = True
) -> None:
    await _persist_dialog_context(session, customer, {PENDING_BOOKING_KEY: draft}, commit=commit)


async def _clear_draft(session: AsyncSession, customer: Customer) -> None:
    ctx_data = dict(customer.dialog_context or {})
    ctx_data.pop(PENDING_BOOKING_KEY, None)
    customer.dialog_context = ctx_data
    await session.commit()


async def _set_manage_after_booking(
    session: AsyncSession,
    customer: Customer,
    appointment_id: int,
    *,
    context_summary: str | None = None,
    commit: bool = True,
) -> None:
    ctx_data = dict(customer.dialog_context or {})
    ctx_data.pop(PENDING_BOOKING_KEY, None)
    ctx_data[DIALOG_MODE_KEY] = DIALOG_MODE_MANAGE
    ctx_data[LAST_APPOINTMENT_ID_KEY] = appointment_id
    if context_summary:
        ctx_data = apply_summary_to_context(ctx_data, context_summary)
    customer.dialog_context = ctx_data
    if commit:
        await session.commit()
    else:
        await session.flush()


async def resolve_tool_mode(
    session: AsyncSession, org_id: int, customer: Customer
) -> str:
    """booking vs manage: active appointment or explicit manage mode."""
    active = await appointment_service.get_active_appointment_for_customer(
        session, org_id, customer.id
    )
    if active is not None:
        return DIALOG_MODE_MANAGE
    ctx_data = customer.dialog_context or {}
    if ctx_data.get(DIALOG_MODE_KEY) == DIALOG_MODE_MANAGE:
        last_id = ctx_data.get(LAST_APPOINTMENT_ID_KEY)
        if last_id is not None:
            appt = await session.get(Appointment, int(last_id))
            if appt is not None and appt.customer_id == customer.id:
                if appt.status in (
                    AppointmentStatus.NEW,
                    AppointmentStatus.CONFIRMED,
                ):
                    return DIALOG_MODE_MANAGE
    return DIALOG_MODE_BOOKING


async def _notify_client_cancel(
    org: Organization, customer: Customer, scheduled_at: datetime, reason: str
) -> None:
    text = notification_service.render_client_cancel_text(scheduled_at, reason)
    await notification_service.send_customer_message(org, customer, text)


async def _notify_client_reschedule(
    org: Organization, customer: Customer, local_dt: datetime
) -> None:
    tz_name = org.timezone or "UTC"
    text = notification_service.render_reschedule_text(local_dt, tz_name)
    await notification_service.send_customer_message(org, customer, text)


def _resolve_booking_date_time(
    date: str,
    time: str,
    tz_name: str,
) -> tuple[str | None, str | None, str | None]:
    """Returns (date_iso, time_hm, error_message)."""
    date_iso = normalize_booking_date(date, tz_name)
    if not date_iso:
        return (
            None,
            None,
            f"Не удалось разобрать дату «{(date or '').strip()}». "
            "Уточни у клиента день записи (например завтра, послезавтра или конкретное число).",
        )
    time_hm = normalize_booking_time(time)
    if not time_hm:
        if is_vague_booking_time(time):
            return None, None, TIME_VAGUE_MESSAGE
        return (
            None,
            None,
            TIME_UNPARSEABLE_MESSAGE.format(time=(time or "").strip()),
        )
    return date_iso, time_hm, None


async def _load_active_staff(session: AsyncSession, org_id: int) -> list[dict]:
    payload = await crm_staff_service.list_crm_staff(session, org_id)
    return [row for row in payload.get("items") or [] if row.get("active", True)]


async def _resolve_doctor_id(
    session: AsyncSession,
    org_id: int,
    explicit_id: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Returns (doctor_id, doctor_name, error_message)."""
    explicit = (explicit_id or "").strip() or None
    staff = await _load_active_staff(session, org_id)
    if not staff:
        return None, None, STAFF_NOT_CONFIGURED_MESSAGE

    if explicit:
        doctor_id, doctor_name = resolve_staff_reference(explicit, staff)
        if doctor_id:
            return doctor_id, doctor_name, None
        return None, None, staff_unknown_choice_message(explicit, staff)

    if len(staff) == 1:
        row = staff[0]
        staff_id = str(row.get("id") or "").strip()
        name = str(row.get("name") or staff_id).strip()
        return staff_id, name, None

    return None, None, staff_choice_prompt_for_llm(staff)


async def _validate_draft_slot(
    session: AsyncSession,
    org: Organization,
    draft: dict,
) -> str | None:
    tz_name = org.timezone or "UTC"
    local_dt, utc_dt = parse_scheduled_local(draft, tz_name)
    doctor_id, doctor_name, err = await _resolve_doctor_id(
        session, org.id, draft.get("doctor_id")
    )
    if err:
        return err
    if not doctor_id:
        return STAFF_NOT_CONFIGURED_MESSAGE
    try:
        await appointment_service.validate_booking_slot_available(
            session,
            org=org,
            doctor_id=doctor_id,
            local_dt=local_dt,
            utc_dt=utc_dt,
        )
    except ValueError as exc:
        return str(exc)
    draft["doctor_id"] = doctor_id
    if doctor_name and not (draft.get("doctor_name") or "").strip():
        draft["doctor_name"] = doctor_name
    return None


async def _create_appointment_from_draft(
    session: AsyncSession,
    *,
    org: Organization,
    customer: Customer,
    draft: dict,
) -> tuple[Appointment, datetime, bool]:
    tz_name = org.timezone or "UTC"
    local_dt, utc_dt = parse_scheduled_local(draft, tz_name)
    schedule_errors = validate_draft_schedule(draft, tz_name)
    if schedule_errors:
        raise ValueError("; ".join(schedule_errors))

    slot_err = await _validate_draft_slot(session, org, draft)
    if slot_err:
        raise ValueError(slot_err)

    doctor_id = (draft.get("doctor_id") or "").strip() or None
    crm_appointment_id = None

    if doctor_id:
        provider = get_crm_provider(org)
        try:
            booking = await provider.book_appointment(
                doctor_id=doctor_id,
                start_iso=local_dt.isoformat(),
                customer_name=customer.name,
                customer_phone=customer.phone,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (409, 422):
                raise ValueError(SLOT_TAKEN_MESSAGE) from exc
            raise
        crm_appointment_id = booking.crm_appointment_id

    try:
        appt = Appointment(
            customer_id=customer.id,
            scheduled_at=utc_dt,
            status=AppointmentStatus.NEW,
            crm_appointment_id=crm_appointment_id,
            crm_doctor_id=doctor_id,
            service_name=(draft.get("service") or "").strip() or None,
            service_price_minor=await resolve_service_price_minor(
                session,
                org.id,
                draft.get("service"),
            ),
        )
        session.add(appt)
        auto_confirmed = await appointment_service.auto_confirm_if_enabled(session, org, appt)
        # Flush only — caller (confirm_pending_booking) commits once at the end of the
        # locked critical section so the customer row lock covers the whole booking write.
        await session.flush()
        await session.refresh(appt)
    except Exception:
        if crm_appointment_id is not None:
            # STOPGAP, not the final fix: this only logs — it does not restore
            # the CRM to its pre-call state. The actual requirement is
            # automatic compensation (call the provider's cancel/remove for
            # crm_appointment_id here) so a local failure never leaves a real
            # booking behind. Blocked until a provider's cancel method is
            # tested as rigorously as its booking call — see "Жёсткий гейт" in
            # docs/macdent-access-request-templates.md. Log loudly for manual
            # reconciliation until then.
            logger.error(
                "Orphaned CRM booking: local persist failed after CRM write succeeded",
                extra={
                    "extra_data": {
                        "event": "orphaned_crm_booking",
                        "org_id": org.id,
                        "customer_id": customer.id,
                        "crm_appointment_id": crm_appointment_id,
                        "crm_doctor_id": doctor_id,
                    }
                },
            )
        raise
    if auto_confirmed:
        confirm_text = notification_service.render_confirm_text(utc_dt)
        await notification_service.send_customer_message(org, customer, confirm_text)
    else:
        await notify_admins_about_new_appointment(
            org_id=org.id,
            customer_id=customer.id,
            when_local=local_dt,
            timezone_name=tz_name,
            local_appointment_id=appt.id,
            crm_appointment_id=crm_appointment_id,
        )
    return appt, local_dt, auto_confirmed


def tool_by_name(ctx: TurnContext, name: str, *, mode: str | None = None):
    """Lookup a tool callable by function name (for tests)."""
    for fn in make_tools(ctx, mode=mode):
        if fn.__name__ == name:
            return fn
    raise KeyError(name)


def make_tools(ctx: TurnContext, *, mode: str | None = None):
    """Замыкание: инструменты LLM с доступом к контексту и БД."""
    effective_mode = mode or DIALOG_MODE_BOOKING

    async def get_available_slots(doctor_id: str, date: str) -> str:
        """Получить доступные окна специалиста или ресурса из CRM.

        Args:
            doctor_id: ID из list_crm_staff; можно пусто, если в org один специалист.
            date: Дата: YYYY-MM-DD или «завтра», «послезавтра», день недели и т.п.
        """
        raw_doctor_id = (doctor_id or "").strip()
        async with AsyncSessionLocal() as session:
            org = await session.get(Organization, ctx.org_id)
            if org is None:
                return "Ошибка: организация не найдена."
            provider = get_crm_provider(org)
            tz_name = org.timezone or "UTC"
            if not (date or "").strip():
                return "Укажи дату записи (можно «завтра» или YYYY-MM-DD)."
            date_clean = normalize_booking_date(date, tz_name)
            if not date_clean:
                return (
                    f"Не удалось разобрать дату «{date.strip()}». "
                    "Спроси клиента, на какой день записать (завтра, конкретное число)."
                )
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("UTC")
            today_local = datetime.now(tz).date()
            day = datetime.strptime(date_clean, "%Y-%m-%d").date()
            if day < today_local:
                return (
                    f"Дата {date_clean} уже прошла. Спроси клиента о записи на сегодня "
                    f"({today_local.isoformat()}) или позже и снова проверь свободные окна."
                )
            resolved_id, resolved_name, resolve_err = await _resolve_doctor_id(
                session, ctx.org_id, raw_doctor_id or None
            )
            if resolve_err:
                return resolve_err
            doctor_id = resolved_id or ""
            try:
                slots = await provider.get_available_slots(
                    doctor_id=doctor_id,
                    date_iso=date_clean,
                    tz_name=tz_name,
                )
            except Exception as exc:
                return f"Не удалось получить окна из CRM: {exc}"
            if not slots:
                return "Свободных окон на выбранную дату нет."
            formatted = ", ".join(slot.start.strftime("%H:%M") for slot in slots[:8])
            staff_label = resolved_name or doctor_id
            staff_note = f" (специалист: {staff_label})" if staff_label else ""
            return (
                f"Свободные окна на {date_clean}{staff_note}: {formatted}. "
                "Предложи клиенту выбрать время."
            )

    async def get_services_info() -> str:
        """Получить каталог услуг организации для ответа клиенту."""
        async with AsyncSessionLocal() as session:
            text = await load_services_catalog_for_org(session, ctx.org_id)
        return (text.strip() if text and text.strip() else EMPTY_SERVICES_CATALOG).strip()

    async def list_crm_staff() -> str:
        """Список специалистов организации для записи."""
        async with AsyncSessionLocal() as session:
            org = await session.get(Organization, ctx.org_id)
            if org is None:
                return "Ошибка: организация не найдена."
            payload = await crm_staff_service.list_crm_staff(session, ctx.org_id)
            items = [row for row in payload.get("items") or [] if row.get("active", True)]
            if not items:
                hint = (payload.get("hint") or "").strip()
                base = (
                    "[Инструкция: специалисты не загружены. Сообщи клиенту, что администратор "
                    "настроит расписание; при необходимости transfer_to_human.]"
                )
                return f"{base} {hint}".strip() if hint else base

            normalized = normalize_staff_items(items)
            if len(normalized) > 1 and ctx.customer_id is not None:
                customer = await _load_customer(session, ctx.customer_id)
                if customer is not None:
                    await _persist_dialog_context(
                        session,
                        customer,
                        {AWAITING_STAFF_CHOICE_KEY: normalized},
                    )
            return format_staff_choice_for_llm(items)

    async def transfer_to_human() -> str:
        """Передать диалог живому сотруднику, если клиент просит человека или нужен нестандартный разбор."""
        ctx.human_transfer_requested = True
        await handoff_service.request_human_handoff(
            org_id=ctx.org_id,
            customer_id=ctx.customer_id,
        )
        return handoff_service.render_handoff_assistant_text()

    async def disable_reminders(disabled: bool = True) -> str:
        """Включить или выключить напоминания о записи за 24 часа и 2 часа.

        Args:
            disabled: True — не присылать напоминания; False — снова включить напоминания.
        """
        async with AsyncSessionLocal() as session:
            customer = await _load_customer(session, ctx.customer_id)
            if customer is None:
                return "Ошибка: профиль клиента не найден."
            customer.disable_reminders = disabled
            await session.commit()
        if disabled:
            return (
                "Напоминания о записи отключены. Сообщите клиенту, что мы больше не будем присылать "
                "напоминания за 24 и 2 часа. Подтверждения, отмены и другие важные сообщения "
                "по-прежнему могут приходить."
            )
        return (
            "Напоминания о записи снова включены. Сообщите клиенту, что мы снова будем напоминать "
            "о визите за 24 и 2 часа до записи."
        )

    async def compress_context(summary: str) -> str:
        """Сохранить краткое summary диалога в БД для экономии токенов в следующих сообщениях.

        Args:
            summary: Краткий текст: имя, услуга, дата/время записи, статус, пожелания клиента.
        """
        try:
            validated = validate_summary_text(summary)
        except ValueError as exc:
            return str(exc)
        async with AsyncSessionLocal() as session:
            customer = await _load_customer(session, ctx.customer_id)
            if customer is None:
                return "Ошибка: профиль клиента не найден."
            customer.dialog_context = apply_summary_to_context(
                customer.dialog_context, validated
            )
            await session.commit()
        return (
            "Контекст сжат и сохранён. В следующих ответах опирайся на summary, "
            "не пересказывай всю переписку заново."
        )

    async def get_customer_context() -> str:
        """Получить сохранённый краткий контекст клиента из БД."""
        async with AsyncSessionLocal() as session:
            customer = await _load_customer(session, ctx.customer_id)
            if customer is None:
                return "Ошибка: профиль клиента не найден."
            summary = get_context_summary(customer.dialog_context)
        if not summary:
            return "Сжатый контекст пока пуст. После записи он создаётся автоматически или вызови compress_context."
        return f"Краткий контекст клиента:\n{summary}"

    async def cancel_appointment(reason: str = "") -> str:
        """Отменить активную запись клиента (статус new или confirmed).

        Args:
            reason: Причина отмены (необязательно; если короткая — подставится стандартная).
        """
        async with AsyncSessionLocal() as session:
            customer = await _load_customer(session, ctx.customer_id)
            if customer is None:
                return "Ошибка: профиль клиента не найден."
            org = await session.get(Organization, ctx.org_id)
            if org is None:
                return "Ошибка: организация не найдена."
            try:
                item, returned_customer = await appointment_service.cancel_appointment_by_customer(
                    session, ctx.org_id, customer.id, reason
                )
            except appointment_service.AppointmentNotFoundError:
                return "Нет активной записи для отмены. Можно оформить новую запись."
            except Exception as exc:
                return f"Не удалось отменить запись: {exc}"
            await session.commit()
            scheduled = item.get("scheduled_at")
            if isinstance(scheduled, str):
                scheduled_dt = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
            else:
                scheduled_dt = scheduled or datetime.now(ZoneInfo("UTC"))
            await _notify_client_cancel(
                org, returned_customer, scheduled_dt, item.get("cancel_reason") or reason
            )
            ctx_data = dict(customer.dialog_context or {})
            ctx_data[DIALOG_MODE_KEY] = DIALOG_MODE_BOOKING
            ctx_data.pop(LAST_APPOINTMENT_ID_KEY, None)
            customer.dialog_context = ctx_data
            await session.commit()
        return (
            f"Запись №{item['id']} отменена. Сообщите клиенту, что визит снят. "
            "Для новой записи собери данные заново."
        )

    async def respond_to_appointment_change(accepted: bool) -> str:
        """Ответить на предложение администратора изменить запись (новое время).

        Args:
            accepted: True — согласен с изменением, False — отказаться (запись отменится).
        """
        async with AsyncSessionLocal() as session:
            customer = await _load_customer(session, ctx.customer_id)
            if customer is None:
                return "Ошибка: профиль клиента не найден."
            org = await session.get(Organization, ctx.org_id)
            if org is None:
                return "Ошибка: организация не найдена."
            tz_name = org.timezone or "UTC"
            try:
                if accepted:
                    item, returned_customer = await appointment_service.accept_appointment_change(
                        session, ctx.org_id, customer.id
                    )
                    await session.commit()
                    scheduled = datetime.fromisoformat(
                        item["scheduled_at"].replace("Z", "+00:00")
                    )
                    msg = notification_service.render_client_change_accepted_text(
                        scheduled, tz_name
                    )
                    await notification_service.send_customer_message(
                        org, returned_customer, msg
                    )
                    return (
                        "Клиент принял изменение. Запись снова ожидает подтверждения администратора. "
                        "Сообщите клиенту, что администратор подтвердит время."
                    )
                item, returned_customer = await appointment_service.reject_appointment_change(
                    session, ctx.org_id, customer.id
                )
                await session.commit()
                scheduled = datetime.fromisoformat(
                    item["scheduled_at"].replace("Z", "+00:00")
                )
                msg = notification_service.render_cancel_text(
                    scheduled,
                    item.get("cancel_reason")
                    or appointment_service.CLIENT_CHANGE_REJECT_REASON,
                )
                await notification_service.send_customer_message(org, returned_customer, msg)
                ctx_data = dict(customer.dialog_context or {})
                ctx_data[DIALOG_MODE_KEY] = DIALOG_MODE_BOOKING
                ctx_data.pop(LAST_APPOINTMENT_ID_KEY, None)
                customer.dialog_context = ctx_data
                await session.commit()
                return (
                    "Запись отменена по отказу клиента. Предложи оформить новую запись при необходимости."
                )
            except appointment_service.AppointmentNotFoundError:
                return (
                    "Нет ожидающего ответа на изменение записи. "
                    "Если клиент согласен с новым временем — попросите написать «да»."
                )
            except appointment_service.InvalidStatusTransitionError:
                return "Срок ответа на изменение истёк. Запись могла быть отменена автоматически."
            except Exception as exc:
                return f"Не удалось обработать ответ: {exc}"

    async def edit_appointment(
        date: str,
        time: str,
        doctor_id: str = "",
    ) -> str:
        """Перенести активную запись на новое время (и опционально другого специалиста).

        Args:
            date: Новая дата (YYYY-MM-DD или «завтра», день недели и т.п.).
            time: Новое время HH:MM (24ч, часовой пояс организации).
            doctor_id: ID специалиста в CRM (пусто — оставить текущего).
        """
        async with AsyncSessionLocal() as session:
            customer = await _load_customer(session, ctx.customer_id)
            if customer is None:
                return "Ошибка: профиль клиента не найден."
            org = await session.get(Organization, ctx.org_id)
            if org is None:
                return "Ошибка: организация не найдена."
            found = await appointment_service.get_active_appointment_for_customer(
                session, ctx.org_id, customer.id
            )
            if found is None:
                return "Нет активной записи для переноса."
            appt, _cust = found
            tz_name = org.timezone or "UTC"
            date_iso, time_hm, resolve_err = _resolve_booking_date_time(date, time, tz_name)
            if resolve_err:
                return resolve_err
            try:
                item, returned_customer, local_dt = await appointment_service.reschedule_appointment(
                    session,
                    ctx.org_id,
                    appt.id,
                    date=date_iso,
                    time=time_hm,
                    doctor_id=doctor_id or None,
                    tz_name=tz_name,
                )
            except ValueError as exc:
                return str(exc)
            except appointment_service.InvalidStatusTransitionError as exc:
                return str(exc)
            except Exception as exc:
                return f"Не удалось перенести запись: {exc}"
            await session.commit()
            await _notify_client_reschedule(org, returned_customer, local_dt)
            await _persist_dialog_context(
                session,
                customer,
                {
                    DIALOG_MODE_KEY: DIALOG_MODE_MANAGE,
                    LAST_APPOINTMENT_ID_KEY: appt.id,
                },
            )
        return (
            f"Запись №{item['id']} перенесена на {local_dt.strftime('%d.%m.%Y %H:%M')} ({tz_name}). "
            "Статус снова «новая» — администратор подтвердит время. Сообщите это клиенту."
        )

    if effective_mode == DIALOG_MODE_MANAGE:
        return [
            cancel_appointment,
            respond_to_appointment_change,
            edit_appointment,
            list_crm_staff,
            get_available_slots,
            get_services_info,
            get_customer_context,
            compress_context,
            disable_reminders,
            transfer_to_human,
        ]

    async def set_customer_name(name: str) -> str:
        """Сохранить имя клиента для записи.

        Args:
            name: Имя клиента (как представился).
        """
        trimmed = (name or "").strip()
        if len(trimmed) < 2:
            return "Имя слишком короткое. Попросите клиента назвать имя полностью."
        async with AsyncSessionLocal() as session:
            customer = await _load_customer(session, ctx.customer_id)
            if customer is None:
                return "Ошибка: профиль клиента не найден."
            customer.name = trimmed
            draft = merge_draft(get_pending_booking(customer.dialog_context), customer_name=trimmed)
            await _persist_draft(session, customer, draft)
        return f"Имя сохранено: {trimmed}. Продолжай сбор данных для записи."

    async def show_appointment_card(
        customer_name: str,
        service: str,
        date: str,
        time: str,
        doctor_id: str = "",
        doctor_name: str = "",
    ) -> str:
        """Показать клиенту карточку записи для проверки. НЕ создаёт запись в БД.

        Вызывай только когда собраны имя, услуга, дата и время. После карточки дождись
        явного подтверждения клиента и вызови confirm_appointment_booking.

        Args:
            customer_name: Имя клиента.
            service: Название услуги из каталога.
            date: Дата (YYYY-MM-DD, «завтра», день недели и т.п.).
            time: Время HH:MM (24ч, часовой пояс организации).
            doctor_id: ID специалиста в CRM (необязательно).
            doctor_name: Имя специалиста для отображения (необязательно).
        """
        async with AsyncSessionLocal() as session:
            customer = await _load_customer(session, ctx.customer_id)
            if customer is None:
                return "Ошибка: профиль клиента не найден."
            org = await session.get(Organization, ctx.org_id)
            if org is None:
                return "Ошибка: организация не найдена."

            tz_name = org.timezone or "UTC"
            date_iso, time_hm, resolve_err = _resolve_booking_date_time(date, time, tz_name)
            if resolve_err:
                return resolve_err

            draft = merge_draft(
                get_pending_booking(customer.dialog_context),
                customer_name=customer_name,
                service=service,
                date=date_iso,
                time=time_hm,
                doctor_id=doctor_id or None,
                doctor_name=doctor_name or None,
            )
            errors = validate_draft_schedule(draft, org.timezone or "UTC")
            if errors:
                # Client-safe: never include tool names (sanitize_client_reply blocks them).
                if len(errors) == 1 and errors[0] == PAST_BOOKING_MESSAGE:
                    return PAST_BOOKING_MESSAGE
                return (
                    "Не хватает данных для карточки: "
                    + ", ".join(errors)
                    + ". Сначала уточни недостающие поля у клиента или выбери дату из свободных окон."
                )

            slot_err = await _validate_draft_slot(session, org, draft)
            if slot_err:
                return slot_err

            if customer.name != draft["customer_name"]:
                customer.name = draft["customer_name"]
            await _persist_draft(session, customer, draft)

            card = render_appointment_card(draft, tz_name=org.timezone or "UTC")
            return (
                f"{card}\n\n"
                "[Инструкция для ассистента: отправь клиенту текст карточки выше дословно. "
                "Не вызывай confirm_appointment_booking, пока клиент явно не подтвердит.]"
            )

    async def confirm_appointment_booking() -> str:
        """Создать запись после явного подтверждения клиентом карточки."""
        result = await booking_service.confirm_pending_booking(
            org_id=ctx.org_id,
            customer_id=ctx.customer_id,
        )
        return booking_service.render_confirm_assistant_text(result)

    async def cancel_appointment_draft() -> str:
        """Отменить черновик записи, если клиент передумал или хочет начать заново."""
        async with AsyncSessionLocal() as session:
            customer = await _load_customer(session, ctx.customer_id)
            if customer is None:
                return "Ошибка: профиль клиента не найден."
            await _clear_draft(session, customer)
        return "Черновик записи отменён. Можно начать новую запись с начала."

    async def save_appointment(date: str, time: str) -> str:
        """Устаревший прямой путь — используй show_appointment_card + confirm_appointment_booking."""
        return (
            "Прямое сохранение отключено. Сначала вызови show_appointment_card с полными данными, "
            "покажи карточку клиенту, дождись «Да» и вызови confirm_appointment_booking."
        )

    async def book_appointment(doctor_id: str, time: str) -> str:
        """Устаревший прямой CRM-путь — используй карточку и confirm_appointment_booking."""
        return (
            "Прямое бронирование отключено. Собери doctor_id и время в show_appointment_card, "
            "покажи карточку, дождись подтверждения и вызови confirm_appointment_booking."
        )

    return [
        set_customer_name,
        get_services_info,
        list_crm_staff,
        get_available_slots,
        show_appointment_card,
        confirm_appointment_booking,
        cancel_appointment_draft,
        get_customer_context,
        compress_context,
        disable_reminders,
        transfer_to_human,
        save_appointment,
        book_appointment,
    ]


async def load_services_catalog_for_org(session: AsyncSession, org_id: int) -> str:
    from bot.services.org_services_catalog import load_services_catalog_for_org as _load

    return await _load(session, org_id)


async def get_or_create_customer_for_channel(
    session: AsyncSession, *, org_id: int, channel_phone: str, name: str | None = None
) -> Customer:
    """channel_phone — стабильный идентификатор: tg:123, web:guest и т.д."""
    stmt = select(Customer).where(Customer.org_id == org_id, Customer.phone == channel_phone)
    r = await session.execute(stmt)
    found = r.scalar_one_or_none()
    if found:
        return found
    c = Customer(org_id=org_id, phone=channel_phone, name=name, dialog_context={})
    session.add(c)
    await session.commit()
    await session.refresh(c)
    return c

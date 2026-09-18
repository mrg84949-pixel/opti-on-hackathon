from datetime import datetime

import httpx
from sqlalchemy import select

from bot.config import settings
from bot.debug_log import debug_log
from bot.db.database import AsyncSessionLocal
from bot.db.models import Admin, Customer, Organization
from bot.logging_config import get_logger

logger = get_logger(__name__)


def _admin_bot_url() -> str | None:
    if not settings.admin_telegram_bot_token:
        return None
    return f"https://api.telegram.org/bot{settings.admin_telegram_bot_token}/sendMessage"


async def _send(chat_id: int, text: str) -> bool:
    url = _admin_bot_url()
    if not url:
        return False
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(url, json={"chat_id": chat_id, "text": text})
        return response.is_success


def _format_new_appointment_text(
    org_name: str,
    customer_name: str,
    customer_phone: str,
    when_local: datetime,
    timezone_name: str,
    local_appointment_id: int,
    crm_appointment_id: str | None,
) -> str:
    crm_part = f"\nCRM ID: {crm_appointment_id}" if crm_appointment_id else ""
    return (
        "Новая запись клиента\n"
        f"Организация: {org_name}\n"
        f"Клиент: {customer_name}\n"
        f"Контакт: {customer_phone}\n"
        f"Когда: {when_local.strftime('%d.%m.%Y %H:%M')} ({timezone_name})\n"
        f"Локальный ID: {local_appointment_id}"
        f"{crm_part}"
    )


def _format_human_transfer_text(
    org_name: str,
    customer_name: str,
    customer_phone: str,
) -> str:
    return (
        "Требуется подключение администратора\n"
        f"Организация: {org_name}\n"
        f"Клиент: {customer_name}\n"
        f"Контакт: {customer_phone}\n"
        "Причина: клиент запросил общение с живым сотрудником.\n"
        "Бот замьючен на 7 дней — снимите мут в панели «Клиенты», когда закончите диалог."
    )


async def notify_admins_about_new_appointment(
    *,
    org_id: int,
    customer_id: int,
    when_local: datetime,
    timezone_name: str,
    local_appointment_id: int,
    crm_appointment_id: str | None = None,
) -> int:
    """
    Отправляет уведомление всем администраторам организации с telegram_id.
    Возвращает количество успешных отправок.
    """
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        customer = await session.get(Customer, customer_id)
        if org is None or customer is None:
            # region agent log
            debug_log(
                run_id="audit-pre",
                hypothesis_id="H3",
                location="bot/notifications/admin_telegram.py:notify:missing_entities",
                message="Organization or customer not found for admin notification",
                data={"org_id": org_id, "has_customer": customer is not None},
            )
            # endregion
            return 0

        stmt = select(Admin).where(Admin.org_id == org_id, Admin.telegram_id.is_not(None))
        admins = (await session.execute(stmt)).scalars().all()
        # region agent log
        debug_log(
            run_id="audit-pre",
            hypothesis_id="H3",
            location="bot/notifications/admin_telegram.py:notify:admins",
            message="Admin notification recipients resolved",
            data={"admins_count": len(admins), "has_bot_token": bool(_admin_bot_url())},
        )
        # endregion
        if not admins:
            return 0

        text = _format_new_appointment_text(
            org_name=org.name,
            customer_name=(customer.name or "Без имени"),
            customer_phone=customer.phone,
            when_local=when_local,
            timezone_name=timezone_name,
            local_appointment_id=local_appointment_id,
            crm_appointment_id=crm_appointment_id,
        )

        sent = 0
        for admin in admins:
            if admin.telegram_id is None:
                continue
            try:
                ok = await _send(int(admin.telegram_id), text)
            except Exception as exc:
                logger.exception(
                    "Admin notification send failed",
                    extra={
                        "extra_data": {
                            "event": "admin_notify_error",
                            "org_id": org_id,
                            "admin_telegram_id": int(admin.telegram_id),
                            "error_type": type(exc).__name__,
                        }
                    },
                )
                ok = False
            if ok:
                sent += 1
        return sent


async def notify_admins_about_human_transfer(*, org_id: int, customer_id: int) -> int:
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        customer = await session.get(Customer, customer_id)
        if org is None or customer is None:
            return 0
        stmt = select(Admin).where(Admin.org_id == org_id, Admin.telegram_id.is_not(None))
        admins = (await session.execute(stmt)).scalars().all()
        if not admins:
            return 0
        text = _format_human_transfer_text(
            org_name=org.name,
            customer_name=(customer.name or "Без имени"),
            customer_phone=customer.phone,
        )
        sent = 0
        for admin in admins:
            if admin.telegram_id is None:
                continue
            try:
                ok = await _send(int(admin.telegram_id), text)
            except Exception as exc:
                logger.exception(
                    "Human transfer notification failed",
                    extra={
                        "extra_data": {
                            "event": "human_transfer_notify_error",
                            "org_id": org_id,
                            "admin_telegram_id": int(admin.telegram_id),
                            "error_type": type(exc).__name__,
                        }
                    },
                )
                ok = False
            if ok:
                sent += 1
        return sent

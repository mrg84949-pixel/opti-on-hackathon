from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.automation.client_change_timeouts import process_client_change_timeouts
from bot.automation.crm_appointment_sync import process_crm_appointment_sync
from bot.automation.reminders import process_24h_reminders, process_2h_reminders
from bot.automation.retention import process_retention_followups
from bot.config import settings

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    scheduler = AsyncIOScheduler(timezone="UTC")
    interval_minutes = settings.reminder_job_interval_minutes
    scheduler.add_job(
        process_24h_reminders,
        trigger="interval",
        minutes=max(1, interval_minutes),
        id="reminders_24h",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        process_2h_reminders,
        trigger="interval",
        minutes=max(1, interval_minutes),
        id="reminders_2h",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        process_client_change_timeouts,
        trigger="interval",
        minutes=max(1, interval_minutes),
        id="client_change_timeouts",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        process_retention_followups,
        trigger="interval",
        minutes=max(1, interval_minutes),
        id="retention_followups",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    crm_interval = settings.crm_sync_interval_minutes
    scheduler.add_job(
        process_crm_appointment_sync,
        trigger="interval",
        minutes=max(1, crm_interval),
        id="crm_appointment_sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None

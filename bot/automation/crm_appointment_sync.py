from __future__ import annotations

from sqlalchemy import select

from bot.db.database import AsyncSessionLocal
from bot.db.models import Organization
from bot.logging_config import get_logger
from bot.services.crm_appointment_sync_service import (
    EXTERNAL_CRM_KEYS,
    _crm_sync_eligible,
    sync_org_crm_cancellations,
)

logger = get_logger(__name__)


async def process_crm_appointment_sync() -> None:
    """Poll external CRMs for cancelled appointments and sync to local bot state."""
    stmt = select(Organization).where(Organization.crm_provider.in_(tuple(EXTERNAL_CRM_KEYS)))
    async with AsyncSessionLocal() as session:
        orgs = (await session.execute(stmt)).scalars().all()
        for org in orgs:
            if not _crm_sync_eligible(org):
                continue
            try:
                result = await sync_org_crm_cancellations(session, org.id)
            except Exception as exc:
                logger.warning(
                    "CRM appointment sync job failed for org",
                    extra={
                        "extra_data": {
                            "event": "crm_sync_job_org_failed",
                            "org_id": org.id,
                            "error": str(exc)[:200],
                        }
                    },
                )
                continue
            if result.get("synced"):
                logger.info(
                    "CRM appointment sync completed",
                    extra={
                        "extra_data": {
                            "event": "crm_sync_job_org_done",
                            "org_id": org.id,
                            "synced": result.get("synced"),
                        }
                    },
                )
        await session.commit()

"""CRM specialists catalog for admin timeline rows."""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.crm.base import StaffMember
from bot.crm.demo_staff import DEFAULT_DEMO_STAFF
from bot.crm.factory import get_crm_provider
from bot.crm.registry import crm_provider_is_demo_mode
from bot.db.models import Organization, OrganizationCrmStaffCache
from bot.logging_config import get_logger
logger = get_logger(__name__)


def _crm_is_demo(org: Organization | None) -> bool:
    if org is None:
        return True
    return crm_provider_is_demo_mode(org)


def _load_staff_from_env() -> list[dict[str, Any]] | None:
    if settings.tenant_config_strict:
        if os.getenv("CRM_STAFF_JSON", "").strip():
            logger.warning(
                "CRM_STAFF_JSON ignored in tenant_config_strict mode",
                extra={"extra_data": {"event": "crm_staff_env_ignored_strict"}},
            )
        return None
    raw = os.getenv("CRM_STAFF_JSON", "").strip()
    if not raw:
        return None
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise ValueError("CRM_STAFF_JSON must be a JSON array")
    return parsed


def _normalize_staff_item(row: dict[str, Any] | StaffMember) -> dict[str, Any]:
    if isinstance(row, StaffMember):
        data: dict[str, Any] = asdict(row)
    else:
        data = dict(row)
        schedule = data.get("schedule")
        if isinstance(schedule, dict):
            if not data.get("work_start"):
                data["work_start"] = schedule.get("start") or schedule.get("work_start")
            if not data.get("work_end"):
                data["work_end"] = schedule.get("end") or schedule.get("work_end")
        active_raw = data.get("active", data.get("is_active", True))
        data["active"] = bool(active_raw)

    staff_id = str(data.get("id") or data.get("doctor_id") or "").strip()
    name = str(data.get("name") or data.get("label") or staff_id).strip()
    return {
        "id": staff_id,
        "name": name or staff_id,
        "work_start": str(data.get("work_start") or "08:00"),
        "work_end": str(data.get("work_end") or "20:00"),
        "active": bool(data.get("active", True)),
    }


def _staff_payload(
    *,
    source: str,
    items: list[dict[str, Any]],
    synced_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "source": source,
        "synced_at": synced_at.isoformat() if synced_at else None,
        "items": items,
    }


async def list_crm_staff(session: AsyncSession, org_id: int) -> dict[str, Any]:
    """Staff rows for timeline + settings UI."""
    org = await session.get(Organization, org_id)
    env_items = _load_staff_from_env()
    if env_items is not None:
        items = [_normalize_staff_item(row) for row in env_items]
        return _staff_payload(source="config", items=items)

    cache = await session.get(OrganizationCrmStaffCache, org_id)
    if cache is not None and cache.items:
        return _staff_payload(source="crm", items=cache.items, synced_at=cache.synced_at)

    if _crm_is_demo(org):
        items = [_normalize_staff_item(row) for row in DEFAULT_DEMO_STAFF]
        return _staff_payload(source="demo", items=items)

    return {
        **_staff_payload(source="crm", items=[]),
        "hint": "Выполните sync для загрузки специалистов из CRM.",
    }


async def sync_crm_staff(session: AsyncSession, org_id: int) -> dict[str, Any]:
    """Pull staff catalog from CRM and persist cache."""
    org = await session.get(Organization, org_id)
    if org is None:
        raise ValueError("Organization not found")

    if _crm_is_demo(org):
        items = [_normalize_staff_item(row) for row in DEFAULT_DEMO_STAFF]
        synced_at = datetime.now(timezone.utc)
        return {
            **_staff_payload(source="demo", items=items, synced_at=synced_at),
            "count": len(items),
        }

    provider = get_crm_provider(org)
    list_staff = getattr(provider, "list_staff", None)
    if list_staff is None:
        raise NotImplementedError("CRM provider does not support staff sync")

    try:
        staff_members = await list_staff()
    except Exception as exc:
        cache = await session.get(OrganizationCrmStaffCache, org_id)
        if cache is not None:
            cache.sync_error = str(exc)
            await session.commit()
        raise

    items = [_normalize_staff_item(member) for member in staff_members]
    synced_at = datetime.now(timezone.utc)
    cache = await session.get(OrganizationCrmStaffCache, org_id)
    if cache is None:
        cache = OrganizationCrmStaffCache(
            organization_id=org_id,
            items=items,
            synced_at=synced_at,
            source="crm",
            sync_error=None,
        )
        session.add(cache)
    else:
        cache.items = items
        cache.synced_at = synced_at
        cache.source = "crm"
        cache.sync_error = None
    await session.commit()

    return {
        **_staff_payload(source="crm", items=items, synced_at=synced_at),
        "count": len(items),
    }

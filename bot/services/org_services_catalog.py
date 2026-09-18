"""Per-org services catalog for LLM get_services_info."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Organization, OrganizationService
from bot.llm.prompts import EMPTY_SERVICES_CATALOG
from bot.services.price_parse import parse_price_label_to_minor


async def resolve_care_message(
    session: AsyncSession,
    org_id: int,
    service_name: str | None,
) -> str | None:
    name = (service_name or "").strip()
    if not name:
        return None
    stmt = select(OrganizationService).where(
        OrganizationService.org_id == org_id,
        OrganizationService.name.ilike(name),
    )
    result = await session.execute(stmt)
    row = result.scalars().first()
    if row is None:
        return None
    text = (row.care_message or "").strip()
    return text or None


async def resolve_service_price_minor(
    session: AsyncSession,
    org_id: int,
    service_name: str | None,
) -> int | None:
    name = (service_name or "").strip()
    if not name:
        return None
    stmt = select(OrganizationService).where(
        OrganizationService.org_id == org_id,
        OrganizationService.name.ilike(name),
    )
    result = await session.execute(stmt)
    row = result.scalars().first()
    if row is None:
        return None
    return parse_price_label_to_minor(row.price_label)


async def list_org_services(session: AsyncSession, org_id: int) -> list[OrganizationService]:
    stmt = (
        select(OrganizationService)
        .where(OrganizationService.org_id == org_id)
        .order_by(OrganizationService.sort_order, OrganizationService.name)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def replace_org_services(
    session: AsyncSession,
    org_id: int,
    items: list[dict[str, object]],
) -> list[OrganizationService]:
    existing = await list_org_services(session, org_id)
    for row in existing:
        await session.delete(row)
    await session.flush()
    created: list[OrganizationService] = []
    for idx, raw in enumerate(items):
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("Service name is required")
        price_label = raw.get("price_label")
        description = raw.get("description")
        care_message = raw.get("care_message")
        sort_order = raw.get("sort_order", idx)
        is_active = raw.get("is_active", True)
        row = OrganizationService(
            org_id=org_id,
            name=name,
            price_label=str(price_label).strip() if price_label else None,
            description=str(description).strip() if description else None,
            care_message=str(care_message).strip() if care_message else None,
            sort_order=int(sort_order) if sort_order is not None else idx,
            is_active=bool(is_active),
        )
        session.add(row)
        created.append(row)
    await session.flush()
    return created


async def list_active_services(session: AsyncSession, org_id: int) -> list[OrganizationService]:
    stmt = (
        select(OrganizationService)
        .where(
            OrganizationService.org_id == org_id,
            OrganizationService.is_active.is_(True),
        )
        .order_by(OrganizationService.sort_order, OrganizationService.name)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def org_has_active_services(session: AsyncSession, org_id: int) -> bool:
    return bool(await list_active_services(session, org_id))


def format_services_catalog_text(services: list[OrganizationService]) -> str:
    lines: list[str] = []
    for svc in services:
        if not svc.is_active:
            continue
        name = svc.name.strip()
        if svc.price_label and svc.price_label.strip():
            lines.append(f"- {name}: {svc.price_label.strip()}")
        else:
            lines.append(f"- {name}")
        if svc.description and svc.description.strip():
            lines.append(f"  {svc.description.strip()}")
    return "\n".join(lines)


async def load_services_catalog_for_org(session: AsyncSession, org_id: int) -> str:
    org = await session.get(Organization, org_id)
    if org is None:
        return EMPTY_SERVICES_CATALOG
    services = await list_active_services(session, org_id)
    if not services:
        return EMPTY_SERVICES_CATALOG
    text = format_services_catalog_text(services)
    return text.strip() if text.strip() else EMPTY_SERVICES_CATALOG

from __future__ import annotations

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Admin,
    Organization,
    PaymentStatus,
    ServiceCatalog,
    ServiceRequest,
    ServiceRequestStatus,
    UserAccount,
)

PROVISIONING_SERVICE_SLUGS = frozenset({"optibot-trial"})
DEFAULT_TRIAL_DAYS = 14


class OrgAlreadyOwnedError(Exception):
    """User already owns an organization."""


def is_provisioning_service(service: ServiceCatalog) -> bool:
    return (service.slug or "").strip().lower() in PROVISIONING_SERVICE_SLUGS


def admin_login_for_user(user: UserAccount) -> str:
    return (user.email or "").strip().lower()


async def user_owns_organization(session: AsyncSession, user_id: int) -> Organization | None:
    return (
        await session.execute(select(Organization).where(Organization.owner_user_id == user_id).limit(1))
    ).scalar_one_or_none()


async def find_open_provisioning_request(
    session: AsyncSession,
    *,
    user_id: int,
    service_id: int,
) -> ServiceRequest | None:
    """Reuse pending trial checkout instead of creating duplicate service_requests."""
    return (
        await session.execute(
            select(ServiceRequest)
            .where(
                ServiceRequest.user_id == user_id,
                ServiceRequest.service_id == service_id,
                ServiceRequest.provisioned_org_id.is_(None),
                ServiceRequest.payment_status == PaymentStatus.PENDING,
                ServiceRequest.status.in_(
                    (ServiceRequestStatus.REQUESTED, ServiceRequestStatus.PROCESSING)
                ),
            )
            .order_by(ServiceRequest.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_organization_core(
    session: AsyncSession,
    *,
    name: str,
    org_timezone: str = "UTC",
    billing_paid_until: datetime | None = None,
    trial_days: int = DEFAULT_TRIAL_DAYS,
    owner_user_id: int | None = None,
    admin_login: str | None = None,
    admin_password_hash: str | None = None,
    admin_telegram_id: int | None = None,
) -> tuple[Organization, Admin | None]:
    if owner_user_id is not None:
        existing_owner_org = await user_owns_organization(session, owner_user_id)
        if existing_owner_org is not None:
            raise OrgAlreadyOwnedError()

    billing_until = billing_paid_until
    if billing_until is None:
        billing_until = datetime.now(dt_timezone.utc) + timedelta(days=trial_days)

    org = Organization(
        name=name.strip(),
        timezone=(org_timezone or "UTC").strip() or "UTC",
        billing_paid_until=billing_until,
        owner_user_id=owner_user_id,
        crm_provider="none",
    )
    session.add(org)
    await session.flush()

    admin: Admin | None = None
    if admin_login and admin_password_hash:
        login_norm = admin_login.strip()
        existing = (
            await session.execute(
                select(Admin).where(Admin.org_id == org.id, Admin.login == login_norm)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Login already taken in organization",
            )
        admin = Admin(
            login=login_norm,
            password_hash=admin_password_hash,
            org_id=org.id,
            telegram_id=admin_telegram_id,
        )
        session.add(admin)
        await session.flush()

    return org, admin


async def create_organization_with_owner(
    session: AsyncSession,
    *,
    name: str,
    owner_user: UserAccount,
    admin_login: str,
    admin_password_hash: str,
    org_timezone: str = "UTC",
    billing_paid_until: datetime | None = None,
    trial_days: int = DEFAULT_TRIAL_DAYS,
) -> tuple[Organization, Admin]:
    try:
        org, admin = await create_organization_core(
            session,
            name=name,
            org_timezone=org_timezone,
            billing_paid_until=billing_paid_until,
            trial_days=trial_days,
            owner_user_id=owner_user.id,
            admin_login=admin_login,
            admin_password_hash=admin_password_hash,
        )
    except OrgAlreadyOwnedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already owns an organization",
        ) from exc
    if admin is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Admin not created")
    return org, admin


def _clinic_name_from_request(req: ServiceRequest) -> str:
    meta = req.meta_json if isinstance(req.meta_json, dict) else {}
    name = str(meta.get("clinic_name") or "").strip()
    if len(name) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="clinic_name is required for provisioning checkout",
        )
    return name


async def provision_org_from_paid_request(
    session: AsyncSession,
    *,
    user: UserAccount,
    req: ServiceRequest,
    service: ServiceCatalog,
) -> tuple[Organization, Admin, bool]:
    """
    Provision org+admin for a paid provisioning service request.
    Returns (org, admin, created) where created=False if already provisioned on this request.
    """
    if req.provisioned_org_id is not None:
        org = await session.get(Organization, req.provisioned_org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provisioned organization missing")
        admin = (
            await session.execute(
                select(Admin).where(Admin.org_id == org.id, Admin.login == admin_login_for_user(user))
            )
        ).scalar_one_or_none()
        if admin is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provisioned admin missing")
        return org, admin, False

    if not is_provisioning_service(service):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Service is not provisionable")

    clinic_name = _clinic_name_from_request(req)
    org, admin = await create_organization_with_owner(
        session,
        name=clinic_name,
        owner_user=user,
        admin_login=admin_login_for_user(user),
        admin_password_hash=user.password_hash,
    )
    req.provisioned_org_id = org.id
    req.status = ServiceRequestStatus.COMPLETED
    await session.flush()
    return org, admin, True

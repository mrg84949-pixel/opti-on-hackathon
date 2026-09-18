from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from bot.billing_access import org_bot_enabled, org_bot_operational, org_subscription_active
from bot.crm import get_crm_provider
from bot.crm.registry import (
    CRM_REGISTRY,
    crm_provider_is_demo_mode,
    crm_without_external_system,
    list_crm_integration_meta,
    merge_crm_config,
    public_crm_config,
    resolve_crm_provider_key,
)
from bot.services.org_secrets import secret_is_set, set_org_secret
from bot.services.outbound_health import channel_send_healthy, clear_channel_health
from bot.channels.whatsapp import (
    resolve_whatsapp_provider,
    send_whatsapp_template,
    send_whatsapp_text,
)
from bot.config import settings, tenant_env_fallback_allowed
from bot.db.database import AsyncSessionLocal
from bot.db.models import Admin, BotInteractionLog, Customer, Organization
from bot.llm import llm_engine
from bot.llm.providers.registry import (
    KNOWN_AI_PROVIDER_KEYS,
    list_ai_integration_meta,
    merge_ai_config,
    platform_credentials_ready,
    public_ai_config,
    resolve_ai_model,
    resolve_ai_provider_key,
)
from bot.logging_config import get_logger
from bot.services import (
    appointment_service,
    crm_appointment_sync_service,
    crm_staff_service,
    customer_service,
    notification_service,
    org_services_catalog,
    stats_service,
)
from bot.services.org_provisioning_service import create_organization_core
from bot.services.telegram_org_service import (
    expected_telegram_webhook_url,
    register_telegram_webhook,
    test_telegram_connection,
)
from bot.services.whatsapp_org_service import test_whatsapp_connection
from web.admin_auth import (
    AdminAuth,
    AmbiguousAdminLoginError,
    authenticate_admin_login,
    clear_admin_session,
    create_admin_session,
    get_admin_auth,
    hash_password,
    require_admin_session,
    require_ops_token,
    resolve_org_id_from_auth,
)
from web.user_auth import issue_token
from web.rate_limit import enforce_auth_limits
router = APIRouter()
logger = get_logger(__name__)




class PromptUpdateRequest(BaseModel):
    system_prompt: str


class BillingUpdateRequest(BaseModel):
    """Дата окончания оплаченного периода (UTC). None — без ограничения по тарифу (бот активен)."""

    billing_paid_until: datetime | None = None


class OrgSettingsPatch(BaseModel):
    bot_enabled: bool | None = None
    auto_confirm_appointments: bool | None = None
    review_2gis_url: str | None = None
    retention_days_after_complete: int | None = Field(default=None, ge=0, le=365)
    retention_message: str | None = None
    post_service_upsell_message: str | None = None
    bot_display_name: str | None = None
    bot_welcome_message: str | None = None
    bot_tone: str | None = None
    ai_provider: str | None = None
    ai_model: str | None = None


class AiIntegrationMetaOut(BaseModel):
    code: str
    display_name: str
    default_model: str
    platform_ready: bool


class AdminLoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)
    org_id: int | None = Field(default=None, ge=1)


class AdminLogoutRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=255)


class AdminCreateRequest(BaseModel):
    login: str = Field(min_length=2, max_length=128)
    password: str = Field(min_length=6, max_length=256)
    telegram_id: int | None = None


class OrganizationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    timezone: str = Field(default="UTC", max_length=64)
    billing_paid_until: datetime | None = None
    initial_admin: AdminCreateRequest | None = None


class CancelAppointmentRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class ProposeAppointmentChangeRequest(BaseModel):
    date: str = Field(min_length=10, max_length=10, description="YYYY-MM-DD")
    time: str = Field(min_length=4, max_length=5, description="HH:MM")
    reason: str = Field(min_length=3, max_length=500)
    doctor_id: str | None = Field(default=None, max_length=64)


class CustomerMuteRequest(BaseModel):
    org_id: int | None = Field(default=None, ge=1)
    days: int | None = Field(default=None, ge=1, le=365)
    muted_until: datetime | None = None


class CustomerMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class CustomerEraseRequest(BaseModel):
    confirm: bool = Field(description="Must be true to erase customer PII")


class BotTestChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    sandbox_id: str | None = Field(default=None, max_length=128)


class BotTestResetRequest(BaseModel):
    sandbox_id: str = Field(min_length=8, max_length=128)


def _normalize_sandbox_id(raw: str | None) -> str:
    sid = (raw or "").strip()
    if not sid:
        # Shorter id: customers.phone is web:admin-sandbox-{id}
        return issue_token()[:16]
    if len(sid) < 8 or len(sid) > 128:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid sandbox_id")
    return sid


def _sandbox_user_id(sandbox_id: str) -> str:
    return f"admin-sandbox-{sandbox_id}"


async def _appointment_action_response(
    session,
    org_id: int,
    item: dict,
    customer: Customer,
    *,
    message: str,
    event: str,
    started: float,
) -> dict:
    org = customer.organization
    notification_sent = False
    if org is not None:
        try:
            result = await notification_service.send_customer_message(org, customer, message)
            notification_sent = bool(result.ok)
            if result.auth_failed:
                logger.warning(
                    "Appointment notification auth failure",
                    extra={
                        "extra_data": {
                            "event": f"{event}_notify_auth_failed",
                            "org_id": org_id,
                            "appointment_id": item.get("id"),
                            "channel": result.channel,
                        }
                    },
                )
        except Exception as exc:
            logger.exception(
                "Appointment notification failed",
                extra={
                    "extra_data": {
                        "event": f"{event}_notify_error",
                        "org_id": org_id,
                        "appointment_id": item.get("id"),
                        "error_type": type(exc).__name__,
                    }
                },
            )
    await session.commit()
    logger.info(
        "Admin appointment action completed",
        extra={
            "extra_data": {
                "event": event,
                "org_id": org_id,
                "appointment_id": item.get("id"),
                "status": item.get("status"),
                "notification_sent": notification_sent,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            }
        },
    )
    return {"item": item, "notification_sent": notification_sent}


def _resolve_org_id(
    auth: AdminAuth = Depends(get_admin_auth),
    x_org_id: int | None = Header(default=None),
) -> int:
    return resolve_org_id_from_auth(auth, x_org_id)


async def _require_active_subscription(org_id: int) -> None:
    """Soft paywall: block write/critical admin actions when tariff expired."""
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        if not org_subscription_active(org):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="subscription_inactive",
            )


def _validate_review_2gis_url(raw: str | None) -> str | None:
    trimmed = (raw or "").strip()
    if not trimmed:
        return None
    if len(trimmed) > 512:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="review_2gis_url must be at most 512 characters",
        )
    lower = trimmed.lower()
    if not lower.startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="review_2gis_url must start with https://",
        )
    return trimmed


def _validate_retention_message(raw: str | None) -> str | None:
    trimmed = (raw or "").strip()
    if not trimmed:
        return None
    if len(trimmed) > 2000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="retention_message must be at most 2000 characters",
        )
    return trimmed


def _validate_post_service_upsell_message(raw: str | None) -> str | None:
    trimmed = (raw or "").strip()
    if not trimmed:
        return None
    if len(trimmed) > 2000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="post_service_upsell_message must be at most 2000 characters",
        )
    return trimmed


def _validate_bot_display_name(raw: str | None) -> str | None:
    trimmed = (raw or "").strip()
    if not trimmed:
        return None
    if len(trimmed) > 128:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bot_display_name must be at most 128 characters",
        )
    return trimmed


def _validate_bot_welcome_message(raw: str | None) -> str | None:
    trimmed = (raw or "").strip()
    if not trimmed:
        return None
    if len(trimmed) > 1000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bot_welcome_message must be at most 1000 characters",
        )
    return trimmed


def _validate_bot_tone(raw: str | None) -> str | None:
    trimmed = (raw or "").strip().lower()
    if not trimmed:
        return None
    from bot.services.bot_branding import ALLOWED_BOT_TONES

    if trimmed not in ALLOWED_BOT_TONES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="bot_tone must be formal or friendly",
        )
    return trimmed


def _validate_ai_provider(raw: str | None) -> str | None:
    if raw is None:
        return None
    trimmed = raw.strip().lower()
    if not trimmed:
        return None
    if trimmed not in KNOWN_AI_PROVIDER_KEYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"ai_provider must be one of: {', '.join(sorted(KNOWN_AI_PROVIDER_KEYS))}",
        )
    return trimmed


def _validate_ai_model(raw: str | None) -> str | None:
    trimmed = (raw or "").strip()
    if not trimmed:
        return None
    if len(trimmed) > 128:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="ai_model must be at most 128 characters",
        )
    return trimmed


def _validate_care_message(raw: str | None) -> str | None:
    trimmed = (raw or "").strip()
    if not trimmed:
        return None
    if len(trimmed) > 2000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="care_message must be at most 2000 characters",
        )
    return trimmed


def _normalize_retention_days(raw: int | None) -> int | None:
    if raw is None:
        return None
    if raw <= 0:
        return 0
    return min(int(raw), 365)


def _secret_is_set(value: str | None) -> bool:
    return bool((value or "").strip())


def _org_settings_payload(org: Organization, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sub_active = org_subscription_active(org, now=now)
    enabled = org_bot_enabled(org)
    effective_key = resolve_ai_provider_key(org)
    return {
        "org_id": org.id,
        "org_name": org.name,
        "bot_enabled": enabled,
        "subscription_active": sub_active,
        "bot_operational": enabled and sub_active,
        "auto_confirm_appointments": bool(getattr(org, "auto_confirm_appointments", False)),
        "review_2gis_url": getattr(org, "review_2gis_url", None),
        "retention_days_after_complete": getattr(org, "retention_days_after_complete", None),
        "retention_message": getattr(org, "retention_message", None),
        "post_service_upsell_message": getattr(org, "post_service_upsell_message", None),
        "bot_display_name": getattr(org, "bot_display_name", None),
        "bot_welcome_message": getattr(org, "bot_welcome_message", None),
        "bot_tone": getattr(org, "bot_tone", None),
        "ai_provider": getattr(org, "ai_provider", None),
        "ai_model": public_ai_config(org).get("model"),
        "ai_provider_effective": effective_key,
        "ai_model_effective": resolve_ai_model(org, effective_key),
        "ai_platform_ready": platform_credentials_ready(effective_key),
    }


@router.get("/stats")
async def get_business_stats(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        response = await stats_service.get_business_stats(session, org_id)
    logger.info(
        "Admin stats resolved",
        extra={"extra_data": {"event": "admin_stats", "org_id": org_id, "duration_ms": round((perf_counter() - started) * 1000, 2)}},
    )
    return response


@router.get("/stats/activity")
async def get_stats_activity(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    """Сводка по записям в разрезе дня / недели / месяца / года."""
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        payload = await stats_service.get_activity_stats(session, org_id)
    logger.info(
        "Admin stats activity resolved",
        extra={"extra_data": {"event": "admin_stats_activity", "org_id": org_id, "duration_ms": round((perf_counter() - started) * 1000, 2)}},
    )
    return payload


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    """Главный экран личного кабинета: записи на подтверждение, чеклист, метрики."""
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        payload = await stats_service.get_dashboard_summary(session, org_id)
    logger.info(
        "Admin dashboard summary resolved",
        extra={
            "extra_data": {
                "event": "admin_dashboard_summary",
                "org_id": org_id,
                "pending_count": payload.get("pending_count"),
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            }
        },
    )
    return payload


@router.get("/customers")
async def list_customers(
    limit: int = 50,
    offset: int = 0,
    database: bool = False,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        if database:
            response = await customer_service.list_customers_database(
                session, org_id, limit=limit, offset=offset
            )
        else:
            response = await customer_service.list_customers(session, org_id, limit=limit, offset=offset)
    logger.info(
        "Admin customers listed",
        extra={"extra_data": {"event": "admin_customers", "org_id": org_id, "database": database, "count": len(response["items"]), "duration_ms": round((perf_counter() - started) * 1000, 2)}},
    )
    return response


@router.get("/customers/{customer_id}/profile")
async def get_customer_profile(
    customer_id: int,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        payload = await customer_service.get_customer_profile(session, customer_id, org_id)
        if payload is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return payload


@router.get("/customers/{customer_id}/conversation")
async def get_customer_conversation(
    customer_id: int,
    limit: int = 100,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    safe_limit = max(1, min(limit, 500))
    async with AsyncSessionLocal() as session:
        payload = await customer_service.get_conversation(
            session, customer_id, org_id, limit=safe_limit
        )
        if payload is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        await session.commit()
    return payload


@router.put("/customers/{customer_id}/mute")
async def mute_customer(
    customer_id: int,
    payload: CustomerMuteRequest,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        result = await customer_service.set_mute(
            session,
            customer_id,
            org_id,
            days=payload.days,
            muted_until=payload.muted_until,
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        await session.commit()
    return result


@router.delete("/customers/{customer_id}/mute")
async def unmute_customer(
    customer_id: int,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        result = await customer_service.clear_mute(session, customer_id, org_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        await session.commit()
    return result


@router.delete("/customers/{customer_id}")
async def erase_customer(
    customer_id: int,
    payload: CustomerEraseRequest,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    """POST-05: erase customer PII (logs, appointments, customer row) within org scope."""
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="confirm must be true",
        )
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        result = await customer_service.erase_customer_for_org(session, customer_id, org_id)
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        await session.commit()
    logger.info(
        "Customer erased",
        extra={
            "extra_data": {
                "event": "customer_erased",
                "org_id": org_id,
                "customer_id": customer_id,
                "logs_removed": result["logs_removed"],
                "appointments_removed": result["appointments_removed"],
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            }
        },
    )
    return result


@router.post("/customers/{customer_id}/message")
async def send_customer_message(
    customer_id: int,
    payload: CustomerMessageRequest,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    """Manual admin outreach to customer (TG/WA)."""
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        result = await customer_service.send_admin_message(
            session, customer_id, org_id, text=payload.message
        )
        if result is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
        if result.get("error_code") == "unsupported_channel":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Customer channel does not support outbound messaging",
            )
        if result.get("auth_failed"):
            logger.warning(
                "Admin customer message auth failure",
                extra={
                    "extra_data": {
                        "event": "admin_customer_message_auth_failed",
                        "org_id": org_id,
                        "customer_id": customer_id,
                        "channel": result.get("channel"),
                    }
                },
            )
        await session.commit()
    logger.info(
        "Admin customer message sent",
        extra={
            "extra_data": {
                "event": "admin_customer_message",
                "org_id": org_id,
                "customer_id": customer_id,
                "notification_sent": result.get("notification_sent"),
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            }
        },
    )
    return result


@router.get("/appointments")
async def list_appointments(
    limit: int = 50,
    offset: int = 0,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        response = await appointment_service.list_appointments(session, org_id, limit=limit, offset=offset)
    logger.info(
        "Admin appointments listed",
        extra={"extra_data": {"event": "admin_appointments", "org_id": org_id, "count": len(response["items"]), "duration_ms": round((perf_counter() - started) * 1000, 2)}},
    )
    return response


@router.get("/appointments/timeline")
async def appointments_timeline(
    date: str | None = None,
    slot_minutes: int = appointment_service.DEFAULT_TIMELINE_SLOT_MINUTES,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        tz_name = org.timezone or "UTC"
        try:
            response = await appointment_service.build_appointments_timeline(
                session,
                org_id,
                date=date,
                slot_minutes=slot_minutes,
                tz_name=tz_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info(
        "Admin appointments timeline",
        extra={
            "extra_data": {
                "event": "admin_appointments_timeline",
                "org_id": org_id,
                "date": response["date"],
                "rows": len(response["rows"]),
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            }
        },
    )
    return response


@router.get("/crm/staff")
async def crm_staff_list(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        try:
            return await crm_staff_service.list_crm_staff(session, org_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/crm/staff/sync")
async def crm_staff_sync(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        try:
            return await crm_staff_service.sync_crm_staff(session, org_id)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except NotImplementedError as exc:
            raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("CRM staff sync failed for org_id=%s", org_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc) or "CRM staff sync failed",
            ) from exc


@router.post("/crm/appointments/sync")
async def crm_appointments_sync(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        try:
            result = await crm_appointment_sync_service.sync_org_crm_cancellations(session, org_id)
            await session.commit()
            return result
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("CRM appointment sync failed for org_id=%s", org_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc) or "CRM appointment sync failed",
            ) from exc


@router.post("/appointments/{appointment_id}/confirm")
async def confirm_appointment(
    appointment_id: int,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    await _require_active_subscription(org_id)
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        try:
            item, customer = await appointment_service.confirm_appointment(session, org_id, appointment_id)
        except appointment_service.AppointmentNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
        except appointment_service.InvalidStatusTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        scheduled_at = datetime.fromisoformat(item["scheduled_at"])
        message = notification_service.render_confirm_text(scheduled_at)
        return await _appointment_action_response(
            session,
            org_id,
            item,
            customer,
            message=message,
            event="admin_appointment_confirm",
            started=started,
        )


@router.post("/appointments/{appointment_id}/cancel")
async def cancel_appointment(
    appointment_id: int,
    payload: CancelAppointmentRequest,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    await _require_active_subscription(org_id)
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        try:
            item, customer = await appointment_service.cancel_appointment(
                session, org_id, appointment_id, payload.reason
            )
        except appointment_service.AppointmentNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
        except appointment_service.InvalidCancelReasonError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except appointment_service.InvalidStatusTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        scheduled_at = datetime.fromisoformat(item["scheduled_at"])
        message = notification_service.render_cancel_text(scheduled_at, item["cancel_reason"] or payload.reason)
        return await _appointment_action_response(
            session,
            org_id,
            item,
            customer,
            message=message,
            event="admin_appointment_cancel",
            started=started,
        )


@router.post("/appointments/{appointment_id}/propose-change")
async def propose_appointment_change(
    appointment_id: int,
    payload: ProposeAppointmentChangeRequest,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        tz_name = org.timezone or "UTC"
        try:
            item, customer, local_dt = await appointment_service.propose_appointment_change(
                session,
                org_id,
                appointment_id,
                date=payload.date,
                time=payload.time,
                reason=payload.reason,
                doctor_id=payload.doctor_id,
                tz_name=tz_name,
            )
        except appointment_service.AppointmentNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
        except appointment_service.ClientChangePendingError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        except appointment_service.InvalidCancelReasonError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        except appointment_service.InvalidStatusTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        message = notification_service.render_admin_change_proposal_text(
            local_dt,
            item.get("client_change_reason") or payload.reason,
            tz_name=tz_name,
            response_hours=settings.client_change_response_hours,
        )
        return await _appointment_action_response(
            session,
            org_id,
            item,
            customer,
            message=message,
            event="admin_appointment_propose_change",
            started=started,
        )


@router.post("/appointments/{appointment_id}/complete")
async def complete_appointment(
    appointment_id: int,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    await _require_active_subscription(org_id)
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        try:
            item, customer = await appointment_service.complete_appointment(session, org_id, appointment_id)
        except appointment_service.AppointmentNotFoundError:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
        except appointment_service.InvalidStatusTransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
        org = customer.organization or await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        care = await org_services_catalog.resolve_care_message(
            session,
            org_id,
            item.get("service_name"),
        )
        message = notification_service.render_complete_text(org, care_message=care)
        return await _appointment_action_response(
            session,
            org_id,
            item,
            customer,
            message=message,
            event="admin_appointment_complete",
            started=started,
        )


@router.get("/prompts")
async def get_org_prompts(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        response = {
            "org_id": org.id,
            "org_name": org.name,
            "system_prompt": org.system_prompt or "",
            "timezone": org.timezone,
        }
        logger.info(
            "Admin prompt loaded",
            extra={
                "extra_data": {
                    "event": "admin_prompt_get",
                    "org_id": org_id,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                }
            },
        )
        return response


@router.put("/prompts")
async def update_org_prompt(
    payload: PromptUpdateRequest,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    started = perf_counter()
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        org.system_prompt = payload.system_prompt.strip()
        await session.commit()
        await session.refresh(org)
    response = {
        "status": "updated",
        "org_id": org.id,
        "system_prompt": org.system_prompt or "",
    }
    logger.info(
        "Admin prompt updated",
        extra={
            "extra_data": {
                "event": "admin_prompt_update",
                "org_id": org_id,
                "prompt_len": len(response["system_prompt"]),
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            }
        },
    )
    return response


@router.post("/organizations", status_code=status.HTTP_201_CREATED)
async def create_organization(
    payload: OrganizationCreateRequest,
    auth: AdminAuth = Depends(get_admin_auth),
):
    require_ops_token(auth)
    async with AsyncSessionLocal() as session:
        initial_admin_row: Admin | None = None
        admin_login: str | None = None
        admin_password_hash: str | None = None
        admin_telegram_id: int | None = None
        if payload.initial_admin is not None:
            admin_login = payload.initial_admin.login.strip()
            admin_password_hash = hash_password(payload.initial_admin.password)
            admin_telegram_id = payload.initial_admin.telegram_id
        org, initial_admin_row = await create_organization_core(
            session,
            name=payload.name,
            org_timezone=payload.timezone,
            billing_paid_until=payload.billing_paid_until,
            owner_user_id=None,
            admin_login=admin_login,
            admin_password_hash=admin_password_hash,
            admin_telegram_id=admin_telegram_id,
        )
        await session.commit()
        await session.refresh(org)
        if initial_admin_row is not None:
            await session.refresh(initial_admin_row)

    now = datetime.now(timezone.utc)
    logger.info(
        "Organization created",
        extra={
            "extra_data": {
                "event": "admin_create_organization",
                "org_id": org.id,
                "has_initial_admin": initial_admin_row is not None,
            }
        },
    )
    return {
        "id": org.id,
        "name": org.name,
        "billing_paid_until": org.billing_paid_until.isoformat() if org.billing_paid_until else None,
        "subscription_active": org_subscription_active(org, now=now),
        "initial_admin": (
            {"id": initial_admin_row.id, "login": initial_admin_row.login, "org_id": org.id}
            if initial_admin_row is not None
            else None
        ),
    }


@router.get("/organizations")
async def list_organizations(auth: AdminAuth = Depends(get_admin_auth)):
    """Список организаций (клиентов B2B) и статус оплаты тарифа."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as session:
        if auth.mode == "session":
            org = await session.get(Organization, auth.org_id) if auth.org_id is not None else None
            rows = [org] if org is not None else []
        else:
            rows = (await session.execute(select(Organization).order_by(Organization.id.asc()))).scalars().all()
    logger.info(
        "Admin organizations listed",
        extra={
            "extra_data": {
                "event": "admin_list_organizations",
                "mode": auth.mode,
                "org_id": auth.org_id,
                "count": len(rows),
            }
        },
    )
    return {
        "items": [
            {
                "id": o.id,
                "name": o.name,
                "billing_paid_until": o.billing_paid_until.isoformat() if o.billing_paid_until else None,
                "subscription_active": org_subscription_active(o, now=now),
            }
            for o in rows
        ]
    }


@router.put("/organizations/{org_id}/billing")
async def update_organization_billing(
    org_id: int,
    payload: BillingUpdateRequest,
    auth: AdminAuth = Depends(get_admin_auth),
):
    require_ops_token(auth)
    logger.info(
        "Admin organization billing update",
        extra={
            "extra_data": {
                "event": "admin_update_billing",
                "org_id": org_id,
                "mode": auth.mode,
            }
        },
    )
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        org.billing_paid_until = payload.billing_paid_until
        await session.commit()
        await session.refresh(org)
        refreshed = org
    now = datetime.now(timezone.utc)
    return {
        "org_id": refreshed.id,
        "billing_paid_until": refreshed.billing_paid_until.isoformat() if refreshed.billing_paid_until else None,
        "subscription_active": org_subscription_active(refreshed, now=now),
    }


@router.get("/interaction-logs")
async def list_interaction_logs(
    limit: int = 100,
    offset: int = 0,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    """Логи обращений к боту по организации (диагностика)."""
    safe_limit = max(1, min(limit, 500))
    safe_offset = max(0, offset)
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        stmt = (
            select(BotInteractionLog)
            .where(BotInteractionLog.org_id == org_id)
            .order_by(BotInteractionLog.created_at.desc())
            .limit(safe_limit)
            .offset(safe_offset)
        )
        rows = (await session.execute(stmt)).scalars().all()
    return {
        "org_id": org_id,
        "items": [
            {
                "id": r.id,
                "channel": r.channel,
                "external_user_id": r.external_user_id,
                "user_message_preview": r.user_message_preview,
                "reply_preview": r.reply_preview,
                "status": r.status,
                "error_hint": r.error_hint,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
        "limit": safe_limit,
        "offset": safe_offset,
    }


def _month_key_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class IntegrationFieldOut(BaseModel):
    name: str
    label: str
    field_type: str
    required: bool


class CrmIntegrationMetaOut(BaseModel):
    code: str
    display_name: str
    docs_url: str | None
    fields: list[IntegrationFieldOut]


class OrgIntegrationsOut(BaseModel):
    org_id: int
    crm_provider: str | None
    crm_base_url: str | None
    crm_config: dict[str, str] | None = None
    crm_api_token_set: bool
    crm_user_token_set: bool
    telegram_bot_token_set: bool
    crm_resolved_provider: str
    crm_demo_mode: bool
    crm_without_external: bool
    telegram_send_healthy: bool
    telegram_expected_webhook_url: str | None
    telegram_token_rotated: bool = False


class OrgIntegrationsIn(BaseModel):
    crm_provider: str | None = None
    crm_base_url: str | None = None
    crm_api_token: str | None = None
    crm_user_token: str | None = None
    company_id: str | None = None
    default_service_id: str | None = None
    telegram_bot_token: str | None = None
    crm_config: dict[str, str] | None = None


class OrgIntegrationsTestCrmOut(BaseModel):
    ok: bool
    demo_mode: bool
    staff_count: int
    message: str


class OrgIntegrationsTestTelegramOut(BaseModel):
    ok: bool
    token_set: bool
    bot_username: str | None
    webhook_url: str | None
    expected_webhook_url: str | None
    message: str


def _org_telegram_connected(org: Organization) -> bool:
    if secret_is_set(org, "telegram_bot_token"):
        return True
    if not tenant_env_fallback_allowed():
        return False
    return bool((settings.telegram_token or "").strip())


def _org_whatsapp_connected(org: Organization) -> bool:
    provider = resolve_whatsapp_provider(org)
    if provider == "green":
        return bool((org.whatsapp_instance_id or "").strip() and secret_is_set(org, "whatsapp_api_token"))
    if provider == "meta":
        return bool(
            (org.whatsapp_meta_phone_number_id or "").strip()
            and secret_is_set(org, "whatsapp_meta_access_token")
        )
    return False


def _org_integrations_out(org: Organization, *, telegram_token_rotated: bool = False) -> OrgIntegrationsOut:
    cfg = public_crm_config(org)
    return OrgIntegrationsOut(
        org_id=org.id,
        crm_provider=org.crm_provider,
        crm_base_url=org.crm_base_url,
        crm_config=cfg or None,
        crm_api_token_set=secret_is_set(org, "crm_api_token"),
        crm_user_token_set=secret_is_set(org, "crm_user_token"),
        telegram_bot_token_set=secret_is_set(org, "telegram_bot_token"),
        crm_resolved_provider=resolve_crm_provider_key(org),
        crm_demo_mode=crm_provider_is_demo_mode(org),
        crm_without_external=crm_without_external_system(org),
        telegram_send_healthy=_org_telegram_connected(org)
        and channel_send_healthy(org.id, "telegram"),
        telegram_expected_webhook_url=expected_telegram_webhook_url(),
        telegram_token_rotated=telegram_token_rotated,
    )


@router.get("/ai-providers", response_model=list[AiIntegrationMetaOut])
async def list_ai_providers(
    _auth: AdminAuth = Depends(get_admin_auth),
):
    return [
        AiIntegrationMetaOut(
            code=meta.code,
            display_name=meta.display_name,
            default_model=meta.default_model,
            platform_ready=meta.platform_ready,
        )
        for meta in list_ai_integration_meta()
    ]


@router.get("/org-integrations/providers", response_model=list[CrmIntegrationMetaOut])
async def list_org_integration_providers(
    _auth: AdminAuth = Depends(get_admin_auth),
):
    return [
        CrmIntegrationMetaOut(
            code=meta.code,
            display_name=meta.display_name,
            docs_url=meta.docs_url,
            fields=[
                IntegrationFieldOut(
                    name=field.name,
                    label=field.label,
                    field_type=field.field_type,
                    required=field.required,
                )
                for field in meta.fields
            ],
        )
        for meta in list_crm_integration_meta()
    ]


@router.get("/org-integrations", response_model=OrgIntegrationsOut)
async def get_org_integrations(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        return _org_integrations_out(org)


@router.patch("/org-integrations", response_model=OrgIntegrationsOut)
async def patch_org_integrations(
    payload: OrgIntegrationsIn,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    await _require_active_subscription(org_id)
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        if payload.crm_provider is not None:
            key = payload.crm_provider.strip().lower()
            if key not in CRM_REGISTRY:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Unknown crm_provider: {payload.crm_provider}",
                )
            org.crm_provider = key if key else None
        if payload.crm_base_url is not None:
            org.crm_base_url = payload.crm_base_url.strip() or None
        if payload.crm_api_token is not None:
            set_org_secret(org, "crm_api_token", payload.crm_api_token)
        if payload.crm_user_token is not None:
            set_org_secret(org, "crm_user_token", payload.crm_user_token)
        if payload.company_id is not None:
            merge_crm_config(org, {"company_id": payload.company_id.strip() or None})
        if payload.default_service_id is not None:
            merge_crm_config(org, {"default_service_id": payload.default_service_id.strip() or None})
        telegram_token_rotated = False
        if payload.telegram_bot_token is not None:
            set_org_secret(org, "telegram_bot_token", payload.telegram_bot_token)
            if (payload.telegram_bot_token or "").strip():
                clear_channel_health(org_id, "telegram")
                telegram_token_rotated = True
        if payload.crm_config is not None:
            merge_crm_config(org, payload.crm_config)
        await session.commit()
        await session.refresh(org)
        return _org_integrations_out(org, telegram_token_rotated=telegram_token_rotated)


@router.post("/org-integrations/test-crm", response_model=OrgIntegrationsTestCrmOut)
async def test_org_crm_connection(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    await _require_active_subscription(org_id)
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        provider = get_crm_provider(org)
        demo_mode = bool(getattr(provider, "demo_mode", False))
        without_external = crm_without_external_system(org)
        try:
            staff = await provider.list_staff()
        except Exception as exc:
            logger.warning(
                "CRM test connection failed",
                extra={
                    "extra_data": {
                        "event": "org_integrations_test_crm_failed",
                        "org_id": org_id,
                        "error": str(exc)[:200],
                    }
                },
            )
            return OrgIntegrationsTestCrmOut(
                ok=False,
                demo_mode=demo_mode,
                staff_count=0,
                message=str(exc)[:300],
            )
        count = len(staff)
        if without_external:
            message = f"Режим без внешней CRM: встроенный каталог специалистов ({count})."
        elif demo_mode:
            message = f"Демо-режим: {count} специалист(ов) из каталога по умолчанию."
        else:
            message = f"Подключение успешно: {count} специалист(ов)."
        return OrgIntegrationsTestCrmOut(
            ok=True,
            demo_mode=demo_mode,
            staff_count=count,
            message=message,
        )


@router.post("/org-integrations/test-telegram", response_model=OrgIntegrationsTestTelegramOut)
async def test_org_telegram_connection(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        result = await test_telegram_connection(org)
        if result.ok:
            clear_channel_health(org_id, "telegram")
        return OrgIntegrationsTestTelegramOut(
            ok=result.ok,
            token_set=result.token_set,
            bot_username=result.bot_username,
            webhook_url=result.webhook_url,
            expected_webhook_url=result.expected_webhook_url,
            message=result.message,
        )


@router.post("/org-integrations/register-telegram-webhook", response_model=OrgIntegrationsTestTelegramOut)
async def register_org_telegram_webhook(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    await _require_active_subscription(org_id)
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        result = await register_telegram_webhook(org)
        if result.ok:
            clear_channel_health(org_id, "telegram")
        return OrgIntegrationsTestTelegramOut(
            ok=result.ok,
            token_set=result.token_set,
            bot_username=result.bot_username,
            webhook_url=result.webhook_url,
            expected_webhook_url=result.expected_webhook_url,
            message=result.message,
        )


class OrgServiceItemOut(BaseModel):
    id: int
    name: str
    price_label: str | None = None
    description: str | None = None
    care_message: str | None = None
    sort_order: int
    is_active: bool


class OrgServicesOut(BaseModel):
    org_id: int
    items: list[OrgServiceItemOut]
    catalog_effective: str
    catalog_source: Literal["org", "empty"]
    services_configured: bool


class OrgServiceItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    price_label: str | None = Field(default=None, max_length=128)
    description: str | None = None
    care_message: str | None = Field(default=None, max_length=2000)
    sort_order: int = 0
    is_active: bool = True


class OrgServicesIn(BaseModel):
    items: list[OrgServiceItemIn] = Field(default_factory=list)


def _org_service_item_out(row) -> OrgServiceItemOut:
    return OrgServiceItemOut(
        id=row.id,
        name=row.name,
        price_label=row.price_label,
        description=row.description,
        care_message=row.care_message,
        sort_order=row.sort_order,
        is_active=row.is_active,
    )


async def _org_services_out(session, org_id: int) -> OrgServicesOut:
    rows = await org_services_catalog.list_org_services(session, org_id)
    configured = await org_services_catalog.org_has_active_services(session, org_id)
    catalog_effective = (await org_services_catalog.load_services_catalog_for_org(session, org_id)).strip()
    return OrgServicesOut(
        org_id=org_id,
        items=[_org_service_item_out(row) for row in rows],
        catalog_effective=catalog_effective,
        catalog_source="org" if configured else "empty",
        services_configured=configured,
    )


@router.get("/org-services", response_model=OrgServicesOut)
async def get_org_services(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        return await _org_services_out(session, org_id)


@router.put("/org-services", response_model=OrgServicesOut)
async def put_org_services(
    payload: OrgServicesIn,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        try:
            rows = await org_services_catalog.replace_org_services(
                session,
                org_id,
                [
                    {
                        **item.model_dump(),
                        "care_message": _validate_care_message(item.care_message),
                    }
                    for item in payload.items
                ],
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        await session.commit()
        for row in rows:
            await session.refresh(row)
        return await _org_services_out(session, org_id)


class WhatsAppSettingsOut(BaseModel):
    org_id: int
    whatsapp_provider: str | None
    whatsapp_instance_id: str | None
    whatsapp_meta_phone_number_id: str | None
    meta_access_token_set: bool
    whatsapp_api_token_set: bool
    whatsapp_meta_reminder_template_name: str | None
    whatsapp_meta_reminder_template_lang: str
    whatsapp_broadcast_quota_monthly: int
    whatsapp_broadcast_sent_count: int
    whatsapp_broadcast_month_key: str | None
    resolved_provider: str
    whatsapp_send_healthy: bool
    whatsapp_token_rotated: bool = False


class WhatsAppSettingsTestOut(BaseModel):
    ok: bool
    provider: str
    message: str
    display_phone: str | None = None


def _whatsapp_settings_out(org: Organization, *, whatsapp_token_rotated: bool = False) -> WhatsAppSettingsOut:
    return WhatsAppSettingsOut(
        org_id=org.id,
        whatsapp_provider=org.whatsapp_provider,
        whatsapp_instance_id=org.whatsapp_instance_id,
        whatsapp_meta_phone_number_id=org.whatsapp_meta_phone_number_id,
        meta_access_token_set=secret_is_set(org, "whatsapp_meta_access_token"),
        whatsapp_api_token_set=secret_is_set(org, "whatsapp_api_token"),
        whatsapp_meta_reminder_template_name=org.whatsapp_meta_reminder_template_name,
        whatsapp_meta_reminder_template_lang=org.whatsapp_meta_reminder_template_lang,
        whatsapp_broadcast_quota_monthly=org.whatsapp_broadcast_quota_monthly,
        whatsapp_broadcast_sent_count=org.whatsapp_broadcast_sent_count,
        whatsapp_broadcast_month_key=org.whatsapp_broadcast_month_key,
        resolved_provider=resolve_whatsapp_provider(org),
        whatsapp_send_healthy=_org_whatsapp_connected(org)
        and channel_send_healthy(org.id, "whatsapp"),
        whatsapp_token_rotated=whatsapp_token_rotated,
    )


class WhatsAppSettingsIn(BaseModel):
    whatsapp_provider: str | None = None
    whatsapp_instance_id: str | None = None
    whatsapp_api_token: str | None = None
    whatsapp_meta_phone_number_id: str | None = None
    whatsapp_meta_access_token: str | None = None
    whatsapp_meta_reminder_template_name: str | None = None
    whatsapp_meta_reminder_template_lang: str | None = None
    whatsapp_broadcast_quota_monthly: int | None = None


@router.get("/whatsapp-settings", response_model=WhatsAppSettingsOut)
async def get_whatsapp_settings(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        return _whatsapp_settings_out(org)


@router.put("/whatsapp-settings", response_model=WhatsAppSettingsOut)
async def put_whatsapp_settings(
    payload: WhatsAppSettingsIn,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        if payload.whatsapp_provider is not None:
            p = payload.whatsapp_provider.strip().lower()
            org.whatsapp_provider = p if p in ("meta", "green") else None
        if payload.whatsapp_instance_id is not None:
            org.whatsapp_instance_id = payload.whatsapp_instance_id.strip() or None
        whatsapp_token_rotated = False
        if payload.whatsapp_api_token is not None:
            set_org_secret(org, "whatsapp_api_token", payload.whatsapp_api_token.strip() or None)
            if (payload.whatsapp_api_token or "").strip():
                clear_channel_health(org_id, "whatsapp")
                whatsapp_token_rotated = True
        if payload.whatsapp_meta_phone_number_id is not None:
            org.whatsapp_meta_phone_number_id = payload.whatsapp_meta_phone_number_id.strip() or None
        if payload.whatsapp_meta_access_token is not None and payload.whatsapp_meta_access_token.strip():
            set_org_secret(org, "whatsapp_meta_access_token", payload.whatsapp_meta_access_token.strip())
            clear_channel_health(org_id, "whatsapp")
            whatsapp_token_rotated = True
        if payload.whatsapp_meta_reminder_template_name is not None:
            org.whatsapp_meta_reminder_template_name = payload.whatsapp_meta_reminder_template_name.strip() or None
        if payload.whatsapp_meta_reminder_template_lang is not None:
            org.whatsapp_meta_reminder_template_lang = (
                (payload.whatsapp_meta_reminder_template_lang or "ru").strip() or "ru"
            )
        if payload.whatsapp_broadcast_quota_monthly is not None:
            org.whatsapp_broadcast_quota_monthly = max(0, int(payload.whatsapp_broadcast_quota_monthly))
        await session.commit()
        await session.refresh(org)
        return _whatsapp_settings_out(org, whatsapp_token_rotated=whatsapp_token_rotated)


@router.post("/whatsapp-settings/test", response_model=WhatsAppSettingsTestOut)
async def test_whatsapp_settings_connection(
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        result = await test_whatsapp_connection(org)
        if result.ok:
            clear_channel_health(org_id, "whatsapp")
        return WhatsAppSettingsTestOut(
            ok=result.ok,
            provider=result.provider,
            message=result.message,
            display_phone=result.display_phone,
        )


class BroadcastRunIn(BaseModel):
    template_name: str | None = None
    language_code: str = "ru"
    body_parameters: list[str] = []
    green_plain_text: str | None = None


@router.post("/broadcast/run")
async def run_broadcast(
    payload: BroadcastRunIn,
    _auth: AdminAuth = Depends(get_admin_auth),
    org_id: int = Depends(_resolve_org_id),
):
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        if org.whatsapp_broadcast_locked:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Broadcast already running")

        mk = _month_key_now()
        if org.whatsapp_broadcast_month_key != mk:
            org.whatsapp_broadcast_month_key = mk
            org.whatsapp_broadcast_sent_count = 0

        stmt_cust = select(Customer).where(Customer.org_id == org_id, Customer.phone.like("wa:%"))
        customers = list((await session.execute(stmt_cust)).scalars().all())
        need = len(customers)
        quota = org.whatsapp_broadcast_quota_monthly
        sent_already = org.whatsapp_broadcast_sent_count
        if need == 0:
            return {"status": "ok", "sent": 0, "skipped": 0, "reason": "no_whatsapp_customers"}
        if sent_already + need > quota:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Monthly quota exceeded: {sent_already}+{need}>{quota}",
            )

        org.whatsapp_broadcast_locked = True
        await session.commit()

    provider = resolve_whatsapp_provider(org)
    sent_ok = 0
    errors: list[str] = []

    try:
        if provider == "meta":
            name = (payload.template_name or "").strip()
            if not name:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="template_name is required for Meta broadcasts",
                )
            lang = (payload.language_code or "ru").strip() or "ru"
            for c in customers:
                digits = "".join(x for x in c.phone if x.isdigit())
                if not digits:
                    errors.append(f"skip customer {c.id}: bad phone")
                    continue
                ok = await send_whatsapp_template(
                    org,
                    digits,
                    template_name=name,
                    language_code=lang,
                    body_parameters=list(payload.body_parameters or []),
                )
                if ok:
                    sent_ok += 1
                else:
                    errors.append(f"customer {c.id}: send failed")
        else:
            text = (payload.green_plain_text or "").strip()
            if not text:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="green_plain_text is required for Green-API broadcasts",
                )
            for c in customers:
                chat_id = c.phone.removeprefix("wa:")
                ok = await send_whatsapp_text(org, chat_id, text)
                if ok:
                    sent_ok += 1
                else:
                    errors.append(f"customer {c.id}: send failed")
    finally:
        async with AsyncSessionLocal() as session:
            org2 = await session.get(Organization, org_id)
            if org2:
                org2.whatsapp_broadcast_locked = False
                mk_now = _month_key_now()
                if org2.whatsapp_broadcast_month_key != mk_now:
                    org2.whatsapp_broadcast_month_key = mk_now
                    org2.whatsapp_broadcast_sent_count = 0
                org2.whatsapp_broadcast_sent_count += sent_ok
                org2.whatsapp_broadcast_month_key = mk_now
                await session.commit()

    return {
        "status": "ok",
        "sent": sent_ok,
        "attempted": need,
        "errors": errors[:20],
    }


@router.post("/admin/auth/login")
async def admin_auth_login(request: Request, payload: AdminLoginRequest):
    await enforce_auth_limits(
        request,
        action="admin_login",
        identifier=(payload.login or "").strip().lower(),
    )
    try:
        admin = await authenticate_admin_login(
            payload.login,
            payload.password,
            org_id=payload.org_id,
        )
    except AmbiguousAdminLoginError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Multiple organizations; specify org_id",
        ) from None
    if admin is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid login or password")
    async with AsyncSessionLocal() as session:
        session_id, expires_at = await create_admin_session(session, admin.id)
        await session.commit()
    return {
        "session_id": session_id,
        "expires_at": expires_at.isoformat(),
        "admin": {"id": admin.id, "login": admin.login, "org_id": admin.org_id},
    }


@router.post("/admin/auth/logout")
async def admin_auth_logout(payload: AdminLogoutRequest):
    async with AsyncSessionLocal() as session:
        await clear_admin_session(session, payload.session_id.strip())
        await session.commit()
    return {"status": "ok"}


@router.get("/org-settings")
async def get_org_settings(
    org_id: int = Depends(_resolve_org_id),
    _auth: AdminAuth = Depends(get_admin_auth),
):
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        return _org_settings_payload(org)


@router.patch("/org-settings")
async def patch_org_settings(
    payload: OrgSettingsPatch,
    org_id: int = Depends(_resolve_org_id),
    auth: AdminAuth = Depends(get_admin_auth),
):
    require_admin_session(auth)
    async with AsyncSessionLocal() as session:
        org = await session.get(Organization, org_id)
        if org is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
        if payload.bot_enabled is not None:
            org.bot_enabled = payload.bot_enabled
        if payload.auto_confirm_appointments is not None:
            org.auto_confirm_appointments = payload.auto_confirm_appointments
        if payload.review_2gis_url is not None:
            org.review_2gis_url = _validate_review_2gis_url(payload.review_2gis_url)
        if payload.retention_days_after_complete is not None:
            org.retention_days_after_complete = _normalize_retention_days(
                payload.retention_days_after_complete
            )
        if payload.retention_message is not None:
            org.retention_message = _validate_retention_message(payload.retention_message)
        if payload.post_service_upsell_message is not None:
            org.post_service_upsell_message = _validate_post_service_upsell_message(
                payload.post_service_upsell_message
            )
        if payload.bot_display_name is not None:
            org.bot_display_name = _validate_bot_display_name(payload.bot_display_name)
        if payload.bot_welcome_message is not None:
            org.bot_welcome_message = _validate_bot_welcome_message(payload.bot_welcome_message)
        if payload.bot_tone is not None:
            org.bot_tone = _validate_bot_tone(payload.bot_tone)
        if payload.ai_provider is not None:
            org.ai_provider = _validate_ai_provider(payload.ai_provider)
        if payload.ai_model is not None:
            merge_ai_config(org, {"model": _validate_ai_model(payload.ai_model)})
        if (
            payload.bot_enabled is None
            and payload.auto_confirm_appointments is None
            and payload.review_2gis_url is None
            and payload.retention_days_after_complete is None
            and payload.retention_message is None
            and payload.post_service_upsell_message is None
            and payload.bot_display_name is None
            and payload.bot_welcome_message is None
            and payload.bot_tone is None
            and payload.ai_provider is None
            and payload.ai_model is None
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "At least one of bot_enabled, auto_confirm_appointments, review_2gis_url, "
                    "retention_days_after_complete, retention_message, post_service_upsell_message, "
                    "bot_display_name, bot_welcome_message, bot_tone, ai_provider, or ai_model is required"
                ),
            )
        await session.commit()
        await session.refresh(org)
        refreshed = org
    return _org_settings_payload(refreshed)


def _admin_public_row(admin: Admin) -> dict:
    return {
        "id": admin.id,
        "login": admin.login,
        "telegram_id": admin.telegram_id,
        "org_id": admin.org_id,
    }


@router.get("/admins")
async def list_org_admins(
    org_id: int = Depends(_resolve_org_id),
    auth: AdminAuth = Depends(get_admin_auth),
):
    require_admin_session(auth)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(Admin).where(Admin.org_id == org_id).order_by(Admin.id.asc())
            )
        ).scalars().all()
    return {"org_id": org_id, "items": [_admin_public_row(a) for a in rows]}


@router.post("/admins", status_code=status.HTTP_201_CREATED)
async def create_org_admin(
    payload: AdminCreateRequest,
    org_id: int = Depends(_resolve_org_id),
    auth: AdminAuth = Depends(get_admin_auth),
):
    require_admin_session(auth)
    login_norm = payload.login.strip()
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(Admin).where(Admin.org_id == org_id, Admin.login == login_norm)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Login already taken")
        admin = Admin(
            login=login_norm,
            password_hash=hash_password(payload.password),
            org_id=org_id,
            telegram_id=payload.telegram_id,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        created = admin
    return _admin_public_row(created)


@router.delete("/admins/{admin_id}")
async def delete_org_admin(
    admin_id: int,
    org_id: int = Depends(_resolve_org_id),
    auth: AdminAuth = Depends(get_admin_auth),
):
    require_admin_session(auth)
    if auth.admin_id == admin_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete your own account")
    async with AsyncSessionLocal() as session:
        target = await session.get(Admin, admin_id)
        if target is None or target.org_id != org_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin not found")
        count = (
            await session.execute(
                select(func.count()).select_from(Admin).where(Admin.org_id == org_id)
            )
        ).scalar_one()
        if count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete the last admin of the organization",
            )
        await session.delete(target)
        await session.commit()
    return {"status": "ok", "deleted_id": admin_id}


@router.post("/bot-test/chat")
async def bot_test_chat(
    payload: BotTestChatRequest,
    org_id: int = Depends(_resolve_org_id),
    _auth: AdminAuth = Depends(get_admin_auth),
):
    await _require_active_subscription(org_id)
    sandbox_id = _normalize_sandbox_id(payload.sandbox_id)
    user_id = _sandbox_user_id(sandbox_id)
    reply = await llm_engine.get_ai_response(
        user_id=user_id,
        user_text=payload.message.strip(),
        db_memory={},
        channel="web",
        org_id=org_id,
    )
    return {"reply": reply, "sandbox_id": sandbox_id, "org_id": org_id}


@router.post("/bot-test/reset")
async def bot_test_reset(
    payload: BotTestResetRequest,
    org_id: int = Depends(_resolve_org_id),
    _auth: AdminAuth = Depends(get_admin_auth),
):
    sandbox_id = _normalize_sandbox_id(payload.sandbox_id)
    user_id = _sandbox_user_id(sandbox_id)
    llm_engine.clear_in_memory_session(channel="web", user_id=user_id, org_id=org_id)
    return {"status": "ok", "sandbox_id": sandbox_id, "org_id": org_id}
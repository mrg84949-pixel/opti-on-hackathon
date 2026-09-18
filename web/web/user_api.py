from __future__ import annotations

import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from bot.config import settings
from bot.db.database import AsyncSessionLocal
from bot.db.models import (
    Admin,
    Organization,
    PaymentEvent,
    PaymentProvider,
    PaymentStatus,
    PaymentTransaction,
    ServiceCatalog,
    ServiceRequest,
    ServiceRequestStatus,
    UserAccount,
    UserEmailVerificationToken,
    UserPasswordResetToken,
    UserSession,
)
from bot.billing_access import org_subscription_active
from bot.logging_config import get_logger
from bot.services.org_provisioning_service import (
    admin_login_for_user,
    find_open_provisioning_request,
    is_provisioning_service,
    provision_org_from_paid_request,
    user_owns_organization,
)
from web.admin_auth import create_admin_session
from web.user_auth import (
    clear_session,
    create_session,
    hash_password,
    hash_secret,
    issue_token,
    normalize_email,
    now_utc,
    validate_email,
    verify_password,
)
from web.emailer import send_email
from web.rate_limit import enforce_auth_limits

router = APIRouter()
logger = get_logger(__name__)

_RESET_CODE_RE = re.compile(r"^\d{6}$")


def _public_frontend_base() -> str:
    """Публичный URL фронта для ссылок в письмах (PUBLIC_APP_URL или FRONTEND_BASE_URL)."""
    base = (settings.public_app_url or settings.frontend_base_url).strip()
    return base.rstrip("/")


class RegisterRequest(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    phone: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class EmailRequest(BaseModel):
    email: str


class TokenConfirmRequest(BaseModel):
    token: str


class ServiceRequestCreateRequest(BaseModel):
    clinic_name: str | None = Field(default=None, min_length=2, max_length=255)
    notes: str | None = None


class CheckoutConfirmRequest(BaseModel):
    success: bool = True
    failure_reason: str | None = None


def _password_ok(password: str) -> bool:
    return len(password) >= 8


async def _require_user(x_user_session: str | None) -> UserAccount:
    if not x_user_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserSession).where(UserSession.session_id == x_user_session))
        user_session = result.scalar_one_or_none()
        if user_session is None or user_session.expires_at <= now_utc():
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        user = await session.get(UserAccount, user_session.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
        return user


@router.post("/auth/register")
async def register(request: Request, payload: RegisterRequest):
    await enforce_auth_limits(request, action="register", identifier=normalize_email(payload.email))
    email = normalize_email(payload.email)
    if not validate_email(email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный email")
    if not _password_ok(payload.password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пароль слишком короткий: минимум 8 символов",
        )

    async with AsyncSessionLocal() as session:
        exists = await session.execute(select(UserAccount).where(UserAccount.email == email))
        if exists.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Этот email уже зарегистрирован")
        user = UserAccount(
            email=email,
            password_hash=hash_password(payload.password),
            full_name=payload.full_name,
            phone=payload.phone,
            is_active=True,
            email_verified=False,
        )
        session.add(user)
        await session.flush()
        verify_token_raw = issue_token()
        session.add(
            UserEmailVerificationToken(
                user_id=user.id,
                token_hash=hash_secret(verify_token_raw),
                expires_at=now_utc() + timedelta(hours=24),
            )
        )
        session_id, _expires_at = await create_session(session, user.id)
        await session.commit()
        send_email(
            to_email=user.email,
            subject="Подтвердите email",
            text=(
                "Спасибо за регистрацию.\n\n"
                f"Ваш verification token: {verify_token_raw}\n"
                "Введите его на странице подтверждения email."
            ),
        )
        return {
            "status": "registered",
            "user": {"id": user.id, "email": user.email, "email_verified": user.email_verified},
            "session_id": session_id,
        }


@router.post("/auth/login")
async def login(request: Request, payload: LoginRequest):
    email = normalize_email(payload.email)
    await enforce_auth_limits(request, action="login", identifier=email)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(UserAccount).where(UserAccount.email == email))
        user = result.scalar_one_or_none()
        if user is None or not verify_password(payload.password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
        session_id, expires_at = await create_session(session, user.id)
        user.last_login_at = now_utc()
        await session.commit()
        return {
            "status": "ok",
            "session_id": session_id,
            "expires_at": expires_at.isoformat(),
            "user": {"id": user.id, "email": user.email, "email_verified": user.email_verified},
        }


@router.post("/auth/logout")
async def logout(x_user_session: str | None = Header(default=None)):
    if not x_user_session:
        return {"status": "ok"}
    async with AsyncSessionLocal() as session:
        await clear_session(session, x_user_session)
        await session.commit()
    return {"status": "ok"}


@router.get("/auth/me")
async def me(x_user_session: str | None = Header(default=None)):
    user = await _require_user(x_user_session)
    owned_org: Organization | None = None
    async with AsyncSessionLocal() as session:
        owned_org = await user_owns_organization(session, user.id)
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "phone": user.phone,
        "email_verified": user.email_verified,
        "owned_organization": (
            {
                "id": owned_org.id,
                "name": owned_org.name,
                "subscription_active": org_subscription_active(owned_org),
                "billing_paid_until": (
                    owned_org.billing_paid_until.isoformat()
                    if owned_org.billing_paid_until is not None
                    else None
                ),
            }
            if owned_org is not None
            else None
        ),
    }


@router.post("/auth/admin-handoff")
async def admin_handoff(x_user_session: str | None = Header(default=None)):
    user = await _require_user(x_user_session)
    async with AsyncSessionLocal() as session:
        owned_org = await user_owns_organization(session, user.id)
        if owned_org is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No owned organization")
        login_norm = admin_login_for_user(user)
        admin = (
            await session.execute(
                select(Admin).where(Admin.org_id == owned_org.id, Admin.login == login_norm)
            )
        ).scalar_one_or_none()
        if admin is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin account not found")
        session_id, expires_at = await create_admin_session(session, admin.id)
        admin_payload = {"id": admin.id, "login": admin.login, "org_id": admin.org_id}
        await session.commit()
    return {
        "status": "ok",
        "session_id": session_id,
        "expires_at": expires_at.isoformat(),
        "admin": admin_payload,
    }


@router.post("/auth/verify-email/request")
async def verify_email_request(request: Request, payload: EmailRequest):
    email = normalize_email(payload.email)
    await enforce_auth_limits(request, action="verify_email", identifier=email)
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(UserAccount).where(UserAccount.email == email))).scalar_one_or_none()
        if user is None:
            return {"status": "sent"}
        raw_token = issue_token()
        session.add(
            UserEmailVerificationToken(
                user_id=user.id,
                token_hash=hash_secret(raw_token),
                expires_at=now_utc() + timedelta(hours=24),
            )
        )
        await session.commit()
    send_email(
        to_email=user.email,
        subject="Повторная верификация email",
        text=(
            "Вы запросили повторное подтверждение email.\n\n"
            f"Ваш verification token: {raw_token}\n"
            "Введите его на странице подтверждения email."
        ),
    )
    return {"status": "sent"}


@router.post("/auth/verify-email/confirm")
async def verify_email_confirm(payload: TokenConfirmRequest):
    token_hash = hash_secret(payload.token)
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(UserEmailVerificationToken).where(UserEmailVerificationToken.token_hash == token_hash)
            )
        ).scalar_one_or_none()
        if row is None or row.used_at is not None or row.expires_at <= now_utc():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid token")
        user = await session.get(UserAccount, row.user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        row.used_at = now_utc()
        user.email_verified = True
        user.email_verified_at = now_utc()
        await session.commit()
    return {"status": "verified"}


@router.post("/auth/forgot-password")
async def forgot_password(request: Request, payload: EmailRequest):
    email = normalize_email(payload.email)
    await enforce_auth_limits(request, action="forgot_password", identifier=email)
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(UserAccount).where(UserAccount.email == email))).scalar_one_or_none()
        if user is None:
            return {"status": "sent"}
        reset_code = f"{secrets.randbelow(1_000_000):06d}"
        session.add(
            UserPasswordResetToken(
                user_id=user.id,
                token_hash=hash_secret(reset_code),
                expires_at=now_utc() + timedelta(hours=1),
            )
        )
        await session.commit()
    frontend_base = _public_frontend_base()
    reset_page_url = f"{frontend_base}/reset-password"
    send_email(
        to_email=user.email,
        subject="Сброс пароля",
        text=(
            "Вы запросили сброс пароля.\n\n"
            f"Код для сброса (действует 1 час): {reset_code}\n\n"
            f"Откройте в браузере страницу восстановления пароля и введите email, код и новый пароль:\n"
            f"{reset_page_url}\n\n"
            "Если это были не вы, просто проигнорируйте письмо."
        ),
    )
    return {"status": "sent"}


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


@router.post("/auth/reset-password")
async def reset_password(request: Request, payload: ResetPasswordRequest):
    email = normalize_email(payload.email)
    await enforce_auth_limits(request, action="reset_password", identifier=email)
    if not _password_ok(payload.new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Пароль слишком короткий: минимум 8 символов",
        )
    if not validate_email(email):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некорректный email")
    code = payload.code.strip()
    if not _RESET_CODE_RE.match(code):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Код должен состоять из 6 цифр")
    code_hash = hash_secret(code)
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(UserAccount).where(UserAccount.email == email))).scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неверный или просроченный код")
        row = (
            await session.execute(
                select(UserPasswordResetToken)
                .where(
                    UserPasswordResetToken.user_id == user.id,
                    UserPasswordResetToken.token_hash == code_hash,
                    UserPasswordResetToken.used_at.is_(None),
                    UserPasswordResetToken.expires_at > now_utc(),
                )
                .order_by(UserPasswordResetToken.id.desc())
            )
        ).scalars().first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Неверный или просроченный код")
        user.password_hash = hash_password(payload.new_password)
        row.used_at = now_utc()
        await session.commit()
    return {"status": "updated"}


@router.get("/auth/oauth/google/start")
async def oauth_google_start():
    client_id = settings.google_oauth_client_id
    redirect_uri = settings.google_oauth_redirect_uri
    if client_id and redirect_uri:
        auth_url = (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={client_id}"
            f"&redirect_uri={redirect_uri}"
            "&response_type=code"
            "&scope=openid%20email%20profile"
            "&access_type=offline"
            "&prompt=consent"
            f"&state={issue_token()[:24]}"
        )
        return {"status": "redirect", "auth_url": auth_url}
    return {"status": "redirect", "auth_url": "https://accounts.google.com/"}


@router.get("/auth/oauth/google/callback")
async def oauth_google_callback(code: str):
    email = f"google_{code[:8]}@example.com"
    async with AsyncSessionLocal() as session:
        user = (await session.execute(select(UserAccount).where(UserAccount.email == email))).scalar_one_or_none()
        if user is None:
            user = UserAccount(
                email=email,
                password_hash=hash_password(issue_token()),
                full_name="Google User",
                email_verified=True,
                oauth_provider="google",
                oauth_subject=code,
            )
            session.add(user)
            await session.flush()
        session_id, expires_at = await create_session(session, user.id)
        await session.commit()
    return {
        "status": "ok",
        "session_id": session_id,
        "expires_at": expires_at.isoformat(),
        "user": {"id": user.id, "email": user.email, "email_verified": user.email_verified},
    }


@router.get("/services")
async def list_services():
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(select(ServiceCatalog).where(ServiceCatalog.is_active.is_(True)).order_by(ServiceCatalog.id.asc()))
        ).scalars().all()
    return {
        "items": [
            {
                "id": svc.id,
                "slug": svc.slug,
                "name": svc.name,
                "description": svc.description,
                "price_minor": svc.price_minor,
                "currency": svc.currency,
            }
            for svc in rows
        ]
    }


@router.post("/services/{service_id}/request")
async def create_service_request(
    service_id: int, payload: ServiceRequestCreateRequest, x_user_session: str | None = Header(default=None)
):
    user = await _require_user(x_user_session)
    async with AsyncSessionLocal() as session:
        service = await session.get(ServiceCatalog, service_id)
        if service is None or not service.is_active:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
        clinic_name = (payload.clinic_name or "").strip()
        if is_provisioning_service(service) and len(clinic_name) < 2:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="clinic_name is required for this service",
            )
        if is_provisioning_service(service):
            owned = await user_owns_organization(session, user.id)
            if owned is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="User already owns an organization",
                )
            existing = await find_open_provisioning_request(
                session,
                user_id=user.id,
                service_id=service.id,
            )
            if existing is not None:
                meta = existing.meta_json if isinstance(existing.meta_json, dict) else {}
                if clinic_name and clinic_name != str(meta.get("clinic_name") or "").strip():
                    meta = {**meta, "clinic_name": clinic_name}
                    existing.meta_json = meta
                    await session.flush()
                await session.commit()
                return {
                    "id": existing.id,
                    "checkout_id": existing.checkout_id,
                    "status": existing.status.value,
                    "payment_status": existing.payment_status.value,
                    "reused": True,
                }
        req = ServiceRequest(
            user_id=user.id,
            service_id=service.id,
            status=ServiceRequestStatus.REQUESTED,
            payment_status=PaymentStatus.PENDING,
            total_minor=service.price_minor,
            currency=service.currency,
            checkout_id=issue_token()[:16],
            meta_json={"clinic_name": clinic_name, "notes": payload.notes or ""},
        )
        session.add(req)
        await session.flush()
        await session.commit()
        return {
            "id": req.id,
            "checkout_id": req.checkout_id,
            "status": req.status.value,
            "payment_status": req.payment_status.value,
            "reused": False,
        }


@router.post("/checkout/{request_id}/create")
async def create_checkout(request_id: int, x_user_session: str | None = Header(default=None)):
    user = await _require_user(x_user_session)
    async with AsyncSessionLocal() as session:
        req = await session.get(ServiceRequest, request_id)
        if req is None or req.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
        existing_tx = (
            await session.execute(
                select(PaymentTransaction)
                .where(
                    PaymentTransaction.service_request_id == req.id,
                    PaymentTransaction.status == PaymentStatus.PENDING,
                )
                .order_by(PaymentTransaction.id.desc())
            )
        ).scalars().first()
        if existing_tx is not None:
            await session.commit()
            return {
                "transaction_id": existing_tx.id,
                "status": existing_tx.status.value,
                "reused": True,
            }
        tx = PaymentTransaction(
            service_request_id=req.id,
            provider=PaymentProvider.SANDBOX,
            provider_payment_id=issue_token()[:24],
            amount_minor=req.total_minor,
            currency=req.currency,
            status=PaymentStatus.PENDING,
            raw_payload_json={"event": "created"},
        )
        session.add(tx)
        await session.flush()
        session.add(
            PaymentEvent(
                payment_transaction_id=tx.id,
                event_type="checkout_created",
                event_payload_json={"request_id": req.id},
            )
        )
        await session.commit()
        return {"transaction_id": tx.id, "status": tx.status.value, "reused": False}


@router.post("/checkout/{request_id}/confirm")
async def confirm_checkout(
    request_id: int, payload: CheckoutConfirmRequest, x_user_session: str | None = Header(default=None)
):
    user = await _require_user(x_user_session)
    async with AsyncSessionLocal() as session:
        req = await session.get(ServiceRequest, request_id)
        if req is None or req.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
        tx = (
            await session.execute(
                select(PaymentTransaction).where(PaymentTransaction.service_request_id == req.id).order_by(PaymentTransaction.id.desc())
            )
        ).scalars().first()
        if tx is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Checkout not created")
        organization_id: int | None = None
        admin_login: str | None = None
        provisioned = False
        if payload.success:
            tx.status = PaymentStatus.PAID
            tx.paid_at = now_utc()
            req.payment_status = PaymentStatus.PAID
            service = await session.get(ServiceCatalog, req.service_id)
            if service is not None and is_provisioning_service(service):
                org, admin, created = await provision_org_from_paid_request(
                    session,
                    user=user,
                    req=req,
                    service=service,
                )
                organization_id = org.id
                admin_login = admin.login
                provisioned = created
            else:
                req.status = ServiceRequestStatus.PROCESSING
        else:
            tx.status = PaymentStatus.FAILED
            tx.failure_reason = payload.failure_reason or "Sandbox decline"
            req.payment_status = PaymentStatus.FAILED
            req.status = ServiceRequestStatus.CANCELLED
        session.add(
            PaymentEvent(
                payment_transaction_id=tx.id,
                event_type="checkout_confirmed",
                event_payload_json={"success": payload.success, "organization_id": organization_id},
            )
        )
        await session.commit()
        return {
            "request_id": req.id,
            "request_status": req.status.value,
            "payment_status": req.payment_status.value,
            "organization_id": organization_id,
            "admin_login": admin_login,
            "provisioned": provisioned,
        }


@router.get("/checkout/{request_id}/status")
async def checkout_status(request_id: int, x_user_session: str | None = Header(default=None)):
    user = await _require_user(x_user_session)
    async with AsyncSessionLocal() as session:
        req = await session.get(ServiceRequest, request_id)
        if req is None or req.user_id != user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
        tx = (
            await session.execute(
                select(PaymentTransaction).where(PaymentTransaction.service_request_id == req.id).order_by(PaymentTransaction.id.desc())
            )
        ).scalars().first()
    return {
        "request_id": req.id,
        "request_status": req.status.value,
        "payment_status": req.payment_status.value,
        "transaction_status": tx.status.value if tx else None,
    }


@router.get("/my/requests")
async def my_requests(x_user_session: str | None = Header(default=None)):
    user = await _require_user(x_user_session)
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ServiceRequest, ServiceCatalog)
                .join(ServiceCatalog, ServiceCatalog.id == ServiceRequest.service_id)
                .where(ServiceRequest.user_id == user.id)
                .order_by(ServiceRequest.id.desc())
            )
        ).all()
    return {
        "items": [
            {
                "id": req.id,
                "service_name": svc.name,
                "total_minor": req.total_minor,
                "currency": req.currency,
                "status": req.status.value,
                "payment_status": req.payment_status.value,
                "checkout_id": req.checkout_id,
                "provisioned_org_id": req.provisioned_org_id,
            }
            for req, svc in rows
        ]
    }

import enum
from datetime import datetime, time, timezone
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.db.database import Base


class AppointmentStatus(str, enum.Enum):
    NEW = "new"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class ServiceRequestStatus(str, enum.Enum):
    DRAFT = "draft"
    REQUESTED = "requested"
    PROCESSING = "processing"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    AUTHORIZED = "authorized"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class PaymentProvider(str, enum.Enum):
    SANDBOX = "sandbox"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    whatsapp_instance_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    whatsapp_api_token: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    telegram_bot_token: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    telegram_bot_token_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    crm_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    crm_base_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    crm_api_token: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    crm_user_token: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    crm_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    billing_paid_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    bot_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_confirm_appointments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    review_2gis_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    retention_days_after_complete: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retention_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    post_service_upsell_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bot_display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    bot_welcome_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bot_tone: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    ai_provider: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ai_config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    whatsapp_provider: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    whatsapp_meta_phone_number_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    whatsapp_meta_access_token: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    whatsapp_meta_reminder_template_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    whatsapp_meta_reminder_template_lang: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ru"
    )
    whatsapp_broadcast_quota_monthly: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    whatsapp_broadcast_sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    whatsapp_broadcast_month_key: Mapped[Optional[str]] = mapped_column(String(7), nullable=True)
    whatsapp_broadcast_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    owner_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )

    owner: Mapped[Optional["UserAccount"]] = relationship(back_populates="owned_organizations")
    admins: Mapped[list["Admin"]] = relationship(back_populates="organization")
    customers: Mapped[list["Customer"]] = relationship(back_populates="organization")
    promotion_triggers: Mapped[list["PromotionTrigger"]] = relationship(
        back_populates="organization"
    )
    crm_staff_cache: Mapped["OrganizationCrmStaffCache | None"] = relationship(
        back_populates="organization", uselist=False
    )
    organization_services: Mapped[list["OrganizationService"]] = relationship(
        back_populates="organization"
    )


class OrganizationCrmStaffCache(Base):
    __tablename__ = "organization_crm_staff_cache"

    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    items: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=lambda: [])
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="crm")
    sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="crm_staff_cache")


class WebhookSeenEvent(Base):
    __tablename__ = "webhook_seen_events"
    __table_args__ = (UniqueConstraint("channel", "event_key", name="uq_webhook_seen_channel_event"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class BotInteractionLog(Base):
    __tablename__ = "bot_interaction_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    external_user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_message_preview: Mapped[str] = mapped_column(String(512), nullable=False)
    reply_preview: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_hint: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class Admin(Base):
    __tablename__ = "admins"
    __table_args__ = (UniqueConstraint("org_id", "login", name="uq_admins_org_login"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    login: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)

    organization: Mapped["Organization"] = relationship(back_populates="admins")
    sessions: Mapped[list["AdminSession"]] = relationship(back_populates="admin")


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(
        ForeignKey("admins.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )

    admin: Mapped["Admin"] = relationship(back_populates="sessions")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("org_id", "phone", name="uq_customers_org_phone"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    dialog_context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: {}
    )
    muted_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    disable_reminders: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    organization: Mapped["Organization"] = relationship(back_populates="customers")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="customer")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[AppointmentStatus] = mapped_column(
        PgEnum(AppointmentStatus, name="appointment_status", create_type=False),
        nullable=False,
        default=AppointmentStatus.NEW,
    )
    crm_appointment_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    crm_doctor_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    reminder_24h_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    reminder_2h_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    cancel_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    client_change_requested_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    client_change_deadline_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    client_change_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    retention_sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    service_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    service_price_minor: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    customer: Mapped["Customer"] = relationship(back_populates="appointments")


class PromotionTrigger(Base):
    __tablename__ = "promotion_triggers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    template_text: Mapped[str] = mapped_column(Text, nullable=False)
    send_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="promotion_triggers")


class UserAccount(Base):
    __tablename__ = "user_accounts"
    __table_args__ = (UniqueConstraint("oauth_provider", "oauth_subject", name="uq_user_oauth_subject"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    oauth_provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    oauth_subject: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    requests: Mapped[list["ServiceRequest"]] = relationship(back_populates="user")
    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user")
    owned_organizations: Mapped[list["Organization"]] = relationship(back_populates="owner")


class UserEmailVerificationToken(Base):
    __tablename__ = "user_email_verification_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class UserPasswordResetToken(Base):
    __tablename__ = "user_password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user: Mapped["UserAccount"] = relationship(back_populates="sessions")


class OrganizationService(Base):
    """Услуги клиники для бота (get_services_info). Не путать с ServiceCatalog (тарифы Opti-Bot)."""

    __tablename__ = "organization_services"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_organization_services_org_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    price_label: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    care_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    organization: Mapped["Organization"] = relationship(back_populates="organization_services")


class ServiceCatalog(Base):
    """Тарифы Opti-Bot на сайте (/services, checkout). Не путать с OrganizationService (каталог бота)."""

    __tablename__ = "service_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="KZT")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    requests: Mapped[list["ServiceRequest"]] = relationship(back_populates="service")


class ServiceRequest(Base):
    __tablename__ = "service_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("service_catalog.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[ServiceRequestStatus] = mapped_column(
        PgEnum(ServiceRequestStatus, name="service_request_status", create_type=False),
        nullable=False,
        default=ServiceRequestStatus.DRAFT,
        index=True,
    )
    payment_status: Mapped[PaymentStatus] = mapped_column(
        PgEnum(PaymentStatus, name="payment_status", create_type=False),
        nullable=False,
        default=PaymentStatus.PENDING,
        index=True,
    )
    total_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="KZT")
    checkout_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    meta_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=lambda: {})
    provisioned_org_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user: Mapped["UserAccount"] = relationship(back_populates="requests")
    service: Mapped["ServiceCatalog"] = relationship(back_populates="requests")
    provisioned_organization: Mapped[Optional["Organization"]] = relationship(
        foreign_keys=[provisioned_org_id]
    )
    transactions: Mapped[list["PaymentTransaction"]] = relationship(back_populates="service_request")


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service_request_id: Mapped[int] = mapped_column(
        ForeignKey("service_requests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[PaymentProvider] = mapped_column(
        PgEnum(PaymentProvider, name="payment_provider", create_type=False),
        nullable=False,
        default=PaymentProvider.SANDBOX,
    )
    provider_payment_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="KZT")
    status: Mapped[PaymentStatus] = mapped_column(
        PgEnum(PaymentStatus, name="payment_transaction_status", create_type=False),
        nullable=False,
        default=PaymentStatus.PENDING,
        index=True,
    )
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    raw_payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=lambda: {})
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    service_request: Mapped["ServiceRequest"] = relationship(back_populates="transactions")
    events: Mapped[list["PaymentEvent"]] = relationship(back_populates="transaction")


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payment_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("payment_transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=lambda: {})
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    transaction: Mapped["PaymentTransaction"] = relationship(back_populates="events")

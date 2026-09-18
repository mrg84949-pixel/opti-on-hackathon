from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

from bot.config import settings
from bot.logging_config import get_logger

logger = get_logger(__name__)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_size=20,
    max_overflow=50,
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def _ensure_billing_and_logs_schema() -> None:
    """create_all не добавляет колонки в уже существующие таблицы — догоняем схему без обязательного alembic."""
    ddl = [
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS billing_paid_until TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS ix_organizations_billing_paid_until ON organizations (billing_paid_until)",
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS bot_enabled BOOLEAN NOT NULL DEFAULT TRUE",
        """
        CREATE TABLE IF NOT EXISTS bot_interaction_logs (
            id SERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            channel VARCHAR(32) NOT NULL,
            external_user_id VARCHAR(255) NOT NULL,
            user_message_preview VARCHAR(512) NOT NULL,
            reply_preview VARCHAR(512) NOT NULL DEFAULT '',
            status VARCHAR(32) NOT NULL,
            error_hint TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_bot_interaction_logs_org_id ON bot_interaction_logs (org_id)",
        "CREATE INDEX IF NOT EXISTS ix_bot_interaction_logs_channel ON bot_interaction_logs (channel)",
        "CREATE INDEX IF NOT EXISTS ix_bot_interaction_logs_external_user_id ON bot_interaction_logs (external_user_id)",
        "CREATE INDEX IF NOT EXISTS ix_bot_interaction_logs_status ON bot_interaction_logs (status)",
        "CREATE INDEX IF NOT EXISTS ix_bot_interaction_logs_created_at ON bot_interaction_logs (created_at)",
        """
        CREATE TABLE IF NOT EXISTS webhook_seen_events (
            id SERIAL PRIMARY KEY,
            channel VARCHAR(32) NOT NULL,
            event_key VARCHAR(256) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_webhook_seen_channel_event UNIQUE (channel, event_key)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_webhook_seen_events_channel ON webhook_seen_events (channel)",
    ]
    wa_ddl = [
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS whatsapp_provider VARCHAR(16)",
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS whatsapp_meta_phone_number_id VARCHAR(64)",
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS whatsapp_meta_access_token VARCHAR(1024)",
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS whatsapp_meta_reminder_template_name VARCHAR(128)",
        (
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS "
            "whatsapp_meta_reminder_template_lang VARCHAR(16) NOT NULL DEFAULT 'ru'"
        ),
        (
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS "
            "whatsapp_broadcast_quota_monthly INTEGER NOT NULL DEFAULT 500"
        ),
        (
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS "
            "whatsapp_broadcast_sent_count INTEGER NOT NULL DEFAULT 0"
        ),
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS whatsapp_broadcast_month_key VARCHAR(7)",
        (
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS "
            "whatsapp_broadcast_locked BOOLEAN NOT NULL DEFAULT false"
        ),
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS telegram_bot_token_hash VARCHAR(64)",
        "CREATE INDEX IF NOT EXISTS ix_organizations_telegram_bot_token_hash ON organizations (telegram_bot_token_hash)",
        "ALTER TABLE organizations ALTER COLUMN telegram_bot_token TYPE VARCHAR(512)",
        "ALTER TABLE organizations ALTER COLUMN whatsapp_api_token TYPE VARCHAR(1024)",
        "ALTER TABLE organizations ALTER COLUMN crm_api_token TYPE VARCHAR(1024)",
        "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS crm_config JSONB",
        """
        CREATE TABLE IF NOT EXISTS organization_services (
            id SERIAL PRIMARY KEY,
            org_id INTEGER NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
            name VARCHAR(255) NOT NULL,
            price_label VARCHAR(128),
            description TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            CONSTRAINT uq_organization_services_org_name UNIQUE (org_id, name)
        )
        """,
        "CREATE INDEX IF NOT EXISTS ix_organization_services_org_id ON organization_services (org_id)",
    ]
    async with engine.begin() as conn:
        for stmt in ddl:
            await conn.execute(text(stmt))
        for stmt in wa_ddl:
            await conn.execute(text(stmt))
    logger.info(
        "Billing and bot_interaction_logs schema ensured",
        extra={"extra_data": {"event": "db_schema_billing_logs"}},
    )


async def _seed_demo_organization_if_empty() -> None:
    from sqlalchemy import select

    from bot.db.models import Organization

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Organization).limit(1))
        if result.scalar_one_or_none() is not None:
            return
        session.add(
            Organization(
                name="Демо-бизнес",
                crm_provider="macdent",
                system_prompt="Помогай клиентам записываться на услуги и передавай сложные вопросы сотруднику.",
                timezone="Asia/Almaty",
            )
        )
        await session.commit()
        logger.info(
            "Demo organization created",
            extra={"extra_data": {"event": "db_seed_demo_org", "default_org_id": 1}},
        )


async def _seed_service_catalog_if_empty() -> None:
    from sqlalchemy import select

    from bot.db.models import ServiceCatalog

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(ServiceCatalog).limit(1))
        if result.scalar_one_or_none() is not None:
            return
        session.add_all(
            [
                ServiceCatalog(
                    slug="consultation",
                    name="Первичная консультация",
                    description="Знакомство с клиентом и сбор исходных требований",
                    price_minor=12000,
                    currency="KZT",
                ),
                ServiceCatalog(
                    slug="standard_service",
                    name="Стандартная услуга",
                    description="Типовая услуга компании средней продолжительности",
                    price_minor=25000,
                    currency="KZT",
                ),
                ServiceCatalog(
                    slug="optibot-trial",
                    name="Opti-Bot Trial",
                    description="14-дневный trial: клиника, админ-панель и бот записи",
                    price_minor=0,
                    currency="KZT",
                ),
            ]
        )
        await session.commit()
        logger.info("Service catalog seeded", extra={"extra_data": {"event": "db_seed_services"}})


async def init_db() -> None:
    from bot.db import models  # noqa: F401 — регистрация моделей в metadata

    # В Docker после `alembic upgrade head` типы ENUM уже в БД; `create_all` снова шлёт CREATE TYPE → DuplicateObjectError.
    # Прод: задайте SKIP_METADATA_CREATE_ALL=1 (см. docker-compose.prod.yml). Локально без Alembic оставьте unset.
    skip_create_all = settings.skip_metadata_create_all
    if not skip_create_all:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database schema synced", extra={"extra_data": {"event": "db_schema_sync"}})
    else:
        logger.info(
            "Skipping metadata create_all (SKIP_METADATA_CREATE_ALL)",
            extra={"extra_data": {"event": "db_schema_sync_skipped"}},
        )
    await _ensure_billing_and_logs_schema()
    await _seed_demo_organization_if_empty()
    await _seed_service_catalog_if_empty()

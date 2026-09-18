"""initial schema

Revision ID: 20260426_0001
Revises:
Create Date: 2026-04-26
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from bot.db.pg_enum import create_postgres_enum_if_not_exists


revision = "20260426_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    create_postgres_enum_if_not_exists(
        bind,
        "appointment_status",
        ("NEW", "CONFIRMED", "CANCELLED", "COMPLETED"),
    )
    appointment_status = postgresql.ENUM(
        "NEW",
        "CONFIRMED",
        "CANCELLED",
        "COMPLETED",
        name="appointment_status",
        create_type=False,
    )

    op.create_table(
        "organizations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("whatsapp_instance_id", sa.String(length=255), nullable=True),
        sa.Column("whatsapp_api_token", sa.String(length=512), nullable=True),
        sa.Column("telegram_bot_token", sa.String(length=255), nullable=True),
        sa.Column("crm_provider", sa.String(length=64), nullable=True),
        sa.Column("crm_base_url", sa.String(length=512), nullable=True),
        sa.Column("crm_api_token", sa.String(length=512), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "admins",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("login", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_admins_login"), "admins", ["login"], unique=True)
    op.create_index(op.f("ix_admins_telegram_id"), "admins", ["telegram_id"], unique=False)

    op.create_table(
        "customers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column(
            "dialog_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "phone", name="uq_customers_org_phone"),
    )
    op.create_index(op.f("ix_customers_phone"), "customers", ["phone"], unique=False)

    op.create_table(
        "appointments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", appointment_status, nullable=False),
        sa.Column("crm_appointment_id", sa.String(length=128), nullable=True),
        sa.Column("crm_doctor_id", sa.String(length=64), nullable=True),
        sa.Column("reminder_24h_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_appointments_customer_id"), "appointments", ["customer_id"], unique=False)
    op.create_index(op.f("ix_appointments_scheduled_at"), "appointments", ["scheduled_at"], unique=False)
    op.create_index(
        op.f("ix_appointments_crm_appointment_id"),
        "appointments",
        ["crm_appointment_id"],
        unique=False,
    )
    op.create_index(op.f("ix_appointments_crm_doctor_id"), "appointments", ["crm_doctor_id"], unique=False)
    op.create_index(
        op.f("ix_appointments_reminder_24h_sent_at"),
        "appointments",
        ["reminder_24h_sent_at"],
        unique=False,
    )

    op.create_table(
        "promotion_triggers",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("template_text", sa.Text(), nullable=False),
        sa.Column("send_time", sa.Time(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_promotion_triggers_org_id"),
        "promotion_triggers",
        ["org_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_promotion_triggers_org_id"), table_name="promotion_triggers")
    op.drop_table("promotion_triggers")

    op.drop_index(op.f("ix_appointments_reminder_24h_sent_at"), table_name="appointments")
    op.drop_index(op.f("ix_appointments_crm_doctor_id"), table_name="appointments")
    op.drop_index(op.f("ix_appointments_crm_appointment_id"), table_name="appointments")
    op.drop_index(op.f("ix_appointments_scheduled_at"), table_name="appointments")
    op.drop_index(op.f("ix_appointments_customer_id"), table_name="appointments")
    op.drop_table("appointments")

    op.drop_index(op.f("ix_customers_phone"), table_name="customers")
    op.drop_table("customers")

    op.drop_index(op.f("ix_admins_telegram_id"), table_name="admins")
    op.drop_index(op.f("ix_admins_login"), table_name="admins")
    op.drop_table("admins")

    op.drop_table("organizations")
    sa.Enum(name="appointment_status").drop(op.get_bind(), checkfirst=True)

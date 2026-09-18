"""user auth services checkout schema

Revision ID: 20260429_0002
Revises: 20260426_0001
Create Date: 2026-04-29
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from bot.db.pg_enum import create_postgres_enum_if_not_exists


revision = "20260429_0002"
down_revision = "20260426_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    create_postgres_enum_if_not_exists(
        bind,
        "service_request_status",
        ("DRAFT", "REQUESTED", "PROCESSING", "COMPLETED", "CANCELLED"),
    )
    create_postgres_enum_if_not_exists(
        bind,
        "payment_status",
        ("PENDING", "AUTHORIZED", "PAID", "FAILED", "CANCELLED", "REFUNDED"),
    )
    create_postgres_enum_if_not_exists(bind, "payment_provider", ("SANDBOX",))
    create_postgres_enum_if_not_exists(
        bind,
        "payment_transaction_status",
        ("PENDING", "AUTHORIZED", "PAID", "FAILED", "CANCELLED", "REFUNDED"),
    )
    service_request_status = postgresql.ENUM(
        "DRAFT", "REQUESTED", "PROCESSING", "COMPLETED", "CANCELLED",
        name="service_request_status",
        create_type=False,
    )
    payment_status = postgresql.ENUM(
        "PENDING", "AUTHORIZED", "PAID", "FAILED", "CANCELLED", "REFUNDED",
        name="payment_status",
        create_type=False,
    )
    payment_provider = postgresql.ENUM("SANDBOX", name="payment_provider", create_type=False)
    payment_tx_status = postgresql.ENUM(
        "PENDING", "AUTHORIZED", "PAID", "FAILED", "CANCELLED", "REFUNDED",
        name="payment_transaction_status",
        create_type=False,
    )

    op.create_table(
        "user_accounts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("email_verified", sa.Boolean(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oauth_provider", sa.String(length=64), nullable=True),
        sa.Column("oauth_subject", sa.String(length=255), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("oauth_provider", "oauth_subject", name="uq_user_oauth_subject"),
    )
    op.create_index(op.f("ix_user_accounts_email"), "user_accounts", ["email"], unique=True)

    op.create_table(
        "user_email_verification_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_email_verification_tokens_user_id"), "user_email_verification_tokens", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_email_verification_tokens_token_hash"), "user_email_verification_tokens", ["token_hash"], unique=True)
    op.create_index(op.f("ix_user_email_verification_tokens_expires_at"), "user_email_verification_tokens", ["expires_at"], unique=False)

    op.create_table(
        "user_password_reset_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_password_reset_tokens_user_id"), "user_password_reset_tokens", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_password_reset_tokens_token_hash"), "user_password_reset_tokens", ["token_hash"], unique=True)
    op.create_index(op.f("ix_user_password_reset_tokens_expires_at"), "user_password_reset_tokens", ["expires_at"], unique=False)

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
    )
    op.create_index(op.f("ix_user_sessions_user_id"), "user_sessions", ["user_id"], unique=False)
    op.create_index(op.f("ix_user_sessions_session_id"), "user_sessions", ["session_id"], unique=True)
    op.create_index(op.f("ix_user_sessions_expires_at"), "user_sessions", ["expires_at"], unique=False)

    op.create_table(
        "service_catalog",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index(op.f("ix_service_catalog_slug"), "service_catalog", ["slug"], unique=True)

    op.create_table(
        "service_requests",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("status", service_request_status, nullable=False),
        sa.Column("payment_status", payment_status, nullable=False),
        sa.Column("total_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("checkout_id", sa.String(length=128), nullable=False),
        sa.Column("meta_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["service_id"], ["service_catalog.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("checkout_id"),
    )
    op.create_index(op.f("ix_service_requests_user_id"), "service_requests", ["user_id"], unique=False)
    op.create_index(op.f("ix_service_requests_service_id"), "service_requests", ["service_id"], unique=False)
    op.create_index(op.f("ix_service_requests_status"), "service_requests", ["status"], unique=False)
    op.create_index(op.f("ix_service_requests_payment_status"), "service_requests", ["payment_status"], unique=False)
    op.create_index(op.f("ix_service_requests_checkout_id"), "service_requests", ["checkout_id"], unique=True)

    op.create_table(
        "payment_transactions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("service_request_id", sa.Integer(), nullable=False),
        sa.Column("provider", payment_provider, nullable=False),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("status", payment_tx_status, nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("raw_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["service_request_id"], ["service_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_payment_id"),
    )
    op.create_index(op.f("ix_payment_transactions_service_request_id"), "payment_transactions", ["service_request_id"], unique=False)
    op.create_index(op.f("ix_payment_transactions_provider_payment_id"), "payment_transactions", ["provider_payment_id"], unique=True)
    op.create_index(op.f("ix_payment_transactions_status"), "payment_transactions", ["status"], unique=False)

    op.create_table(
        "payment_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("payment_transaction_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("event_payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_transaction_id"], ["payment_transactions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_payment_events_payment_transaction_id"), "payment_events", ["payment_transaction_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_events_payment_transaction_id"), table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_index(op.f("ix_payment_transactions_status"), table_name="payment_transactions")
    op.drop_index(op.f("ix_payment_transactions_provider_payment_id"), table_name="payment_transactions")
    op.drop_index(op.f("ix_payment_transactions_service_request_id"), table_name="payment_transactions")
    op.drop_table("payment_transactions")
    op.drop_index(op.f("ix_service_requests_checkout_id"), table_name="service_requests")
    op.drop_index(op.f("ix_service_requests_payment_status"), table_name="service_requests")
    op.drop_index(op.f("ix_service_requests_status"), table_name="service_requests")
    op.drop_index(op.f("ix_service_requests_service_id"), table_name="service_requests")
    op.drop_index(op.f("ix_service_requests_user_id"), table_name="service_requests")
    op.drop_table("service_requests")
    op.drop_index(op.f("ix_service_catalog_slug"), table_name="service_catalog")
    op.drop_table("service_catalog")
    op.drop_index(op.f("ix_user_sessions_expires_at"), table_name="user_sessions")
    op.drop_index(op.f("ix_user_sessions_session_id"), table_name="user_sessions")
    op.drop_index(op.f("ix_user_sessions_user_id"), table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_index(op.f("ix_user_password_reset_tokens_expires_at"), table_name="user_password_reset_tokens")
    op.drop_index(op.f("ix_user_password_reset_tokens_token_hash"), table_name="user_password_reset_tokens")
    op.drop_index(op.f("ix_user_password_reset_tokens_user_id"), table_name="user_password_reset_tokens")
    op.drop_table("user_password_reset_tokens")
    op.drop_index(op.f("ix_user_email_verification_tokens_expires_at"), table_name="user_email_verification_tokens")
    op.drop_index(op.f("ix_user_email_verification_tokens_token_hash"), table_name="user_email_verification_tokens")
    op.drop_index(op.f("ix_user_email_verification_tokens_user_id"), table_name="user_email_verification_tokens")
    op.drop_table("user_email_verification_tokens")
    op.drop_index(op.f("ix_user_accounts_email"), table_name="user_accounts")
    op.drop_table("user_accounts")
    sa.Enum(name="payment_transaction_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="payment_provider").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="payment_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="service_request_status").drop(op.get_bind(), checkfirst=True)

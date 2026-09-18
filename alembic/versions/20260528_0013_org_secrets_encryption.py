"""organization secret columns widen + telegram_bot_token_hash

Revision ID: 20260528_0013
Revises: 20260528_0012
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0013"
down_revision = "20260528_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("telegram_bot_token_hash", sa.String(length=64), nullable=True))
    op.create_index(
        op.f("ix_organizations_telegram_bot_token_hash"),
        "organizations",
        ["telegram_bot_token_hash"],
        unique=False,
    )
    op.alter_column(
        "organizations",
        "telegram_bot_token",
        existing_type=sa.String(length=255),
        type_=sa.String(length=512),
        existing_nullable=True,
    )
    op.alter_column(
        "organizations",
        "whatsapp_api_token",
        existing_type=sa.String(length=512),
        type_=sa.String(length=1024),
        existing_nullable=True,
    )
    op.alter_column(
        "organizations",
        "crm_api_token",
        existing_type=sa.String(length=512),
        type_=sa.String(length=1024),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "organizations",
        "crm_api_token",
        existing_type=sa.String(length=1024),
        type_=sa.String(length=512),
        existing_nullable=True,
    )
    op.alter_column(
        "organizations",
        "whatsapp_api_token",
        existing_type=sa.String(length=1024),
        type_=sa.String(length=512),
        existing_nullable=True,
    )
    op.alter_column(
        "organizations",
        "telegram_bot_token",
        existing_type=sa.String(length=512),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.drop_index(op.f("ix_organizations_telegram_bot_token_hash"), table_name="organizations")
    op.drop_column("organizations", "telegram_bot_token_hash")

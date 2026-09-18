"""whatsapp meta fields and broadcast quota

Revision ID: 20260507_0004
Revises: 20260503_0003
Create Date: 2026-05-07
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260507_0004"
down_revision = "20260503_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("whatsapp_provider", sa.String(length=16), nullable=True))
    op.add_column(
        "organizations", sa.Column("whatsapp_meta_phone_number_id", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "organizations", sa.Column("whatsapp_meta_access_token", sa.String(length=1024), nullable=True)
    )
    op.add_column(
        "organizations",
        sa.Column("whatsapp_meta_reminder_template_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("whatsapp_meta_reminder_template_lang", sa.String(length=16), nullable=False, server_default="ru"),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "whatsapp_broadcast_quota_monthly",
            sa.Integer(),
            nullable=False,
            server_default="500",
        ),
    )
    op.add_column(
        "organizations",
        sa.Column(
            "whatsapp_broadcast_sent_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "organizations", sa.Column("whatsapp_broadcast_month_key", sa.String(length=7), nullable=True)
    )
    op.add_column(
        "organizations",
        sa.Column(
            "whatsapp_broadcast_locked",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "whatsapp_broadcast_locked")
    op.drop_column("organizations", "whatsapp_broadcast_month_key")
    op.drop_column("organizations", "whatsapp_broadcast_sent_count")
    op.drop_column("organizations", "whatsapp_broadcast_quota_monthly")
    op.drop_column("organizations", "whatsapp_meta_reminder_template_lang")
    op.drop_column("organizations", "whatsapp_meta_reminder_template_name")
    op.drop_column("organizations", "whatsapp_meta_access_token")
    op.drop_column("organizations", "whatsapp_meta_phone_number_id")
    op.drop_column("organizations", "whatsapp_provider")

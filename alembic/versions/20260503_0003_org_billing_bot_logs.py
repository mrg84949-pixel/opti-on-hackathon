"""organization billing and bot interaction logs

Revision ID: 20260503_0003
Revises: 20260429_0002
Create Date: 2026-05-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260503_0003"
down_revision = "20260429_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("billing_paid_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_organizations_billing_paid_until"),
        "organizations",
        ["billing_paid_until"],
        unique=False,
    )
    op.create_table(
        "bot_interaction_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column("user_message_preview", sa.String(length=512), nullable=False),
        sa.Column("reply_preview", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_hint", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bot_interaction_logs_org_id"), "bot_interaction_logs", ["org_id"], unique=False)
    op.create_index(op.f("ix_bot_interaction_logs_channel"), "bot_interaction_logs", ["channel"], unique=False)
    op.create_index(
        op.f("ix_bot_interaction_logs_external_user_id"),
        "bot_interaction_logs",
        ["external_user_id"],
        unique=False,
    )
    op.create_index(op.f("ix_bot_interaction_logs_status"), "bot_interaction_logs", ["status"], unique=False)
    op.create_index(
        op.f("ix_bot_interaction_logs_created_at"), "bot_interaction_logs", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_bot_interaction_logs_created_at"), table_name="bot_interaction_logs")
    op.drop_index(op.f("ix_bot_interaction_logs_status"), table_name="bot_interaction_logs")
    op.drop_index(op.f("ix_bot_interaction_logs_external_user_id"), table_name="bot_interaction_logs")
    op.drop_index(op.f("ix_bot_interaction_logs_channel"), table_name="bot_interaction_logs")
    op.drop_index(op.f("ix_bot_interaction_logs_org_id"), table_name="bot_interaction_logs")
    op.drop_table("bot_interaction_logs")
    op.drop_index(op.f("ix_organizations_billing_paid_until"), table_name="organizations")
    op.drop_column("organizations", "billing_paid_until")

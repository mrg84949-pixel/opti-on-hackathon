"""webhook_seen_events for inbound webhook dedup

Revision ID: 20260528_0012
Revises: 20260528_0011
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0012"
down_revision = "20260528_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "webhook_seen_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("event_key", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("channel", "event_key", name="uq_webhook_seen_channel_event"),
    )
    op.create_index("ix_webhook_seen_events_channel", "webhook_seen_events", ["channel"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_webhook_seen_events_channel", table_name="webhook_seen_events")
    op.drop_table("webhook_seen_events")

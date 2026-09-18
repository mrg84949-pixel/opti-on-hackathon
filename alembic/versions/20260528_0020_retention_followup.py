"""retention follow-up: org settings + appointment completed_at

Revision ID: 20260528_0020
Revises: 20260528_0019
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0020"
down_revision = "20260528_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("retention_days_after_complete", sa.Integer(), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("retention_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("retention_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_appointments_completed_at", "appointments", ["completed_at"], unique=False)
    op.create_index("ix_appointments_retention_sent_at", "appointments", ["retention_sent_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_appointments_retention_sent_at", table_name="appointments")
    op.drop_index("ix_appointments_completed_at", table_name="appointments")
    op.drop_column("appointments", "retention_sent_at")
    op.drop_column("appointments", "completed_at")
    op.drop_column("organizations", "retention_message")
    op.drop_column("organizations", "retention_days_after_complete")

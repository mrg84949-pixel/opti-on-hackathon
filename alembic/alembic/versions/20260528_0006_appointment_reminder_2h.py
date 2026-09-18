"""appointment reminder_2h_sent_at column

Revision ID: 20260528_0006
Revises: 20260528_0005
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0006"
down_revision = "20260528_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("reminder_2h_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_appointments_reminder_2h_sent_at",
        "appointments",
        ["reminder_2h_sent_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_appointments_reminder_2h_sent_at", table_name="appointments")
    op.drop_column("appointments", "reminder_2h_sent_at")

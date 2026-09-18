"""appointment client change response fields

Revision ID: 20260528_0010
Revises: 20260528_0009
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0010"
down_revision = "20260528_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("client_change_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("client_change_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("client_change_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_appointments_client_change_deadline_at"),
        "appointments",
        ["client_change_deadline_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_appointments_client_change_deadline_at"), table_name="appointments")
    op.drop_column("appointments", "client_change_reason")
    op.drop_column("appointments", "client_change_deadline_at")
    op.drop_column("appointments", "client_change_requested_at")

"""organizations auto_confirm_appointments flag

Revision ID: 20260609_0018
Revises: 20260609_0017
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260609_0018"
down_revision = "20260609_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("auto_confirm_appointments", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("organizations", "auto_confirm_appointments", server_default=None)


def downgrade() -> None:
    op.drop_column("organizations", "auto_confirm_appointments")

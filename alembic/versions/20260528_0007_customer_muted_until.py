"""customers.muted_until for admin mute / no-show auto-mute

Revision ID: 20260528_0007
Revises: 20260528_0006
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0007"
down_revision = "20260528_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_customers_muted_until", "customers", ["muted_until"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_customers_muted_until", table_name="customers")
    op.drop_column("customers", "muted_until")

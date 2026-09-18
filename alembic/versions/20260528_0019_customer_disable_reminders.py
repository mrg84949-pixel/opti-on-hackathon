"""customers.disable_reminders for client opt-out of scheduled reminders

Revision ID: 20260528_0019
Revises: 20260609_0018
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0019"
down_revision = "20260609_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("disable_reminders", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("customers", "disable_reminders", server_default=None)


def downgrade() -> None:
    op.drop_column("customers", "disable_reminders")

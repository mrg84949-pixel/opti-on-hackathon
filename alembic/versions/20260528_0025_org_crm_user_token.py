"""org crm user token for YClients admin API

Revision ID: 20260528_0025
Revises: 20260528_0024
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0025"
down_revision = "20260528_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("crm_user_token", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "crm_user_token")

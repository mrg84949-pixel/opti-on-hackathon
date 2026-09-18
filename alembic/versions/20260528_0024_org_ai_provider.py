"""org ai provider fields

Revision ID: 20260528_0024
Revises: 20260528_0023
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260528_0024"
down_revision = "20260528_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("ai_provider", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("ai_config", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "ai_config")
    op.drop_column("organizations", "ai_provider")

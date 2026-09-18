"""organizations.review_2gis_url

Revision ID: 20260528_0011
Revises: 20260528_0010
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0011"
down_revision = "20260528_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("review_2gis_url", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "review_2gis_url")

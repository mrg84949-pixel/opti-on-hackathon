"""post-service upsell and per-service care messages

Revision ID: 20260528_0022
Revises: 20260528_0021
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0022"
down_revision = "20260528_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organization_services", sa.Column("care_message", sa.Text(), nullable=True))
    op.add_column(
        "organizations",
        sa.Column("post_service_upsell_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "post_service_upsell_message")
    op.drop_column("organization_services", "care_message")

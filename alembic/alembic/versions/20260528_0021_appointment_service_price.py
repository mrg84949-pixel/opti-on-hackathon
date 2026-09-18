"""appointment service name and price snapshot for revenue stats

Revision ID: 20260528_0021
Revises: 20260528_0020
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0021"
down_revision = "20260528_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("appointments", sa.Column("service_name", sa.String(length=255), nullable=True))
    op.add_column("appointments", sa.Column("service_price_minor", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("appointments", "service_price_minor")
    op.drop_column("appointments", "service_name")

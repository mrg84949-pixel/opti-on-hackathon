"""organization_services per-org bot catalog

Revision ID: 20260528_0015
Revises: 20260528_0014
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0015"
down_revision = "20260528_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_services",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("price_label", sa.String(length=128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "name", name="uq_organization_services_org_name"),
    )
    op.create_index("ix_organization_services_org_id", "organization_services", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_organization_services_org_id", table_name="organization_services")
    op.drop_table("organization_services")

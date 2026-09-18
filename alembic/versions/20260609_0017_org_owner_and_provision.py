"""organizations owner_user_id and service_requests provisioned_org_id

Revision ID: 20260609_0017
Revises: 20260609_0016
Create Date: 2026-06-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260609_0017"
down_revision = "20260609_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("owner_user_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_organizations_owner_user_id",
        "organizations",
        "user_accounts",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_organizations_owner_user_id"), "organizations", ["owner_user_id"], unique=False)

    op.add_column("service_requests", sa.Column("provisioned_org_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_service_requests_provisioned_org_id",
        "service_requests",
        "organizations",
        ["provisioned_org_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_service_requests_provisioned_org_id"),
        "service_requests",
        ["provisioned_org_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_service_requests_provisioned_org_id"), table_name="service_requests")
    op.drop_constraint("fk_service_requests_provisioned_org_id", "service_requests", type_="foreignkey")
    op.drop_column("service_requests", "provisioned_org_id")

    op.drop_index(op.f("ix_organizations_owner_user_id"), table_name="organizations")
    op.drop_constraint("fk_organizations_owner_user_id", "organizations", type_="foreignkey")
    op.drop_column("organizations", "owner_user_id")

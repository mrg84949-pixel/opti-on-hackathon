"""admins login unique per org_id

Revision ID: 20260609_0016
Revises: 20260528_0015
Create Date: 2026-06-09
"""

from __future__ import annotations

from alembic import op


revision = "20260609_0016"
down_revision = "20260528_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_admins_login", table_name="admins")
    op.create_index("ix_admins_login", "admins", ["login"], unique=False)
    op.create_unique_constraint("uq_admins_org_login", "admins", ["org_id", "login"])


def downgrade() -> None:
    op.drop_constraint("uq_admins_org_login", "admins", type_="unique")
    op.drop_index("ix_admins_login", table_name="admins")
    op.create_index("ix_admins_login", "admins", ["login"], unique=True)

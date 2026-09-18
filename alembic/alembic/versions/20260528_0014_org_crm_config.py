"""organizations.crm_config JSONB

Revision ID: 20260528_0014
Revises: 20260528_0013
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260528_0014"
down_revision = "20260528_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("crm_config", JSONB, nullable=True))
    op.execute(
        """
        UPDATE organizations
        SET crm_config = '{}'::jsonb
        WHERE crm_provider = 'amocrm' AND crm_config IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("organizations", "crm_config")

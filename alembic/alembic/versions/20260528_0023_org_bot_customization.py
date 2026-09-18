"""org bot customization fields

Revision ID: 20260528_0023
Revises: 20260528_0022
Create Date: 2026-05-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260528_0023"
down_revision = "20260528_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("bot_display_name", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("bot_welcome_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "organizations",
        sa.Column("bot_tone", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("organizations", "bot_tone")
    op.drop_column("organizations", "bot_welcome_message")
    op.drop_column("organizations", "bot_display_name")

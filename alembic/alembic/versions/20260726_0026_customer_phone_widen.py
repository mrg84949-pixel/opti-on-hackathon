"""Widen customers.phone for web:admin-sandbox-* and long channel ids

Revision ID: 20260726_0026
Revises: 20260528_0025
Create Date: 2026-07-26

Sandbox bot-test phones are web:admin-sandbox-{token} (~50 chars) and exceeded
VARCHAR(32), causing StringDataRightTruncationError and silent bot failures.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260726_0026"
down_revision = "20260528_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "customers",
        "phone",
        existing_type=sa.String(length=32),
        type_=sa.String(length=128),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "customers",
        "phone",
        existing_type=sa.String(length=128),
        type_=sa.String(length=32),
        existing_nullable=False,
    )

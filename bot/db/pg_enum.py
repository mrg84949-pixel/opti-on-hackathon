"""Idempotent PostgreSQL ENUM creation for Alembic (checkfirst=True is unreliable on some binds)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection


def create_postgres_enum_if_not_exists(connection: Connection, name: str, labels: tuple[str, ...]) -> None:
    values_sql = ", ".join(f"'{label}'" for label in labels)
    connection.execute(
        text(
            f"""
            DO $do$ BEGIN
                CREATE TYPE {name} AS ENUM ({values_sql});
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END $do$;
            """
        )
    )

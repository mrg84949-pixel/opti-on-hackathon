from __future__ import annotations

from logging.config import fileConfig
import os
import asyncio

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool
from sqlalchemy.ext.asyncio import create_async_engine

from bot.db.database import Base
from bot.db import models  # noqa: F401

load_dotenv()

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url"))


def _run_migrations_with_connection(sync_connection) -> None:
    context.configure(connection=sync_connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    db_url = _database_url()
    if "+asyncpg" in db_url:
        async_engine = create_async_engine(db_url, poolclass=pool.NullPool)

        async def _run() -> None:
            async with async_engine.connect() as connection:
                await connection.run_sync(_run_migrations_with_connection)
            await async_engine.dispose()

        asyncio.run(_run())
        return

    alembic_config = config.get_section(config.config_ini_section, {})
    alembic_config["sqlalchemy.url"] = db_url
    connectable = engine_from_config(
        alembic_config,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        _run_migrations_with_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

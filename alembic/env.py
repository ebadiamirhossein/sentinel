"""Alembic environment — async engine, URL from the environment only."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from sentinel.core.config import Secrets

# Importing the models registers every table on Base.metadata, which is what
# --autogenerate diffs against. Keep this import even though it looks unused.
from sentinel.storage import models as _models  # noqa: F401
from sentinel.storage.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `%` is an interpolation character in alembic.ini values — escape it so passwords
# containing '%' survive.
_database_url = Secrets().database_url
config.set_main_option("sqlalchemy.url", _database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

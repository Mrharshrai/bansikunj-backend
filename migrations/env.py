"""Alembic environment, wired to the application's async engine and settings.

Alembic runs migrations through the same ``create_async_engine`` stack the app
uses, so there is exactly one place where the connection is configured.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.core.database import Base

# Import model modules here so ``Base.metadata`` is populated before
# autogenerate runs. Every new model must be added to ``app.models.__init__``.
from app import models  # noqa: F401  (imported for its side effect on Base.metadata)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()

# An explicitly configured URL wins. The test suite passes
# ``TEST_DATABASE_URL`` through the Alembic config (``conftest.py`` does
# ``cfg.set_main_option("sqlalchemy.url", ...)``); unconditionally overwriting
# it here would migrate the *development* database during a test run and leave
# the test database without a schema.
if not config.get_main_option("sqlalchemy.url", None):
    config.set_main_option("sqlalchemy.url", settings.sqlalchemy_database_uri)

#: The URL actually in force, for both the online and offline paths.
database_url = config.get_main_option("sqlalchemy.url")

target_metadata = Base.metadata


def _include_object(object: object, name: str, type_: str, reflected: bool, compare_to: object) -> bool:
    """Keep autogenerate focused on tables this project owns."""
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DBAPI connection (``alembic upgrade --sql``)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

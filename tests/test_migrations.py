"""Verifies the hand-written migration matches the SQLAlchemy models.

``0001_init.py`` was written by hand, so this file is the safety net: it
compares the live schema (after the migrations have run) against
``Base.metadata``. Any divergence fails the suite instead of surfacing later as
a production ``alembic check`` failure.
"""
from __future__ import annotations

import pytest

from app.core.database import Base

pytestmark = pytest.mark.asyncio


def _delete_rule(raw) -> str:
    """Normalise ``pg_constraint.confdeltype`` to a one-letter code.

    asyncpg returns the internal ``"char"`` type as ``bytes``, so a raw
    comparison against ``"c"`` would never match.
    """
    return raw.decode() if isinstance(raw, bytes) else raw


async def test_migration_matches_models(engine):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    async with engine.connect() as conn:

        def _compare(sync_connection) -> list:
            context = MigrationContext.configure(sync_connection)
            return compare_metadata(context, Base.metadata)

        diffs = await conn.run_sync(_compare)

    readable = [
        f"{d.get('type')}: {d.get('name') or d.get('table_name')}"
        + (f" -> {d.get('column_name')}" if d.get("column_name") else "")
        for d in diffs
    ]
    assert not diffs, "models drifted from the migration:\n  " + "\n  ".join(readable)


async def test_expected_tables_exist(engine):
    expected = {
        "users",
        "addresses",
        "refresh_tokens",
        "categories",
        "products",
        "alembic_version",
    }
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    actual = {r[0] for r in rows}
    assert expected <= actual, f"missing tables: {expected - actual}"


async def test_user_role_is_a_native_enum_with_expected_values(engine):
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'user_role' ORDER BY e.enumsortorder"
        )
    labels = {r[0] for r in rows}
    assert labels == {"customer", "admin", "staff", "delivery_agent"}


async def test_single_use_token_hashes_are_indexed(engine):
    """The verification lookup filters on a digest, so the column is indexed.

    The index is deliberately *not* unique: the column is nullable and holds a
    SHA-256 digest of a 256-bit random token, so duplicates are not a realistic
    concern and a unique index would buy nothing. What matters is that the
    lookup is indexed at all.
    """
    async with engine.connect() as conn:
        rows = await conn.exec_driver_sql(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'users' AND indexname = ANY(ARRAY["
            "  'ix_users_email_verification_token_hash',"
            "  'ix_users_password_reset_token_hash'"
            "]) ORDER BY indexname"
        )
    indexed = {r[0] for r in rows}
    assert indexed == {
        "ix_users_email_verification_token_hash",
        "ix_users_password_reset_token_hash",
    }, f"missing single-use token indexes, found {indexed}"


async def test_foreign_keys_are_enforced(engine):
    """Deleting a user must cascade to addresses and refresh tokens."""
    async with engine.connect() as conn:
        ddl = await conn.exec_driver_sql(
            "SELECT conname, confdeltype FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid = 'addresses'::regclass"
        )
    assert ddl, "addresses.user_id foreign key is missing"
    for name, delete_rule in ddl:
        # pg_constraint.confdeltype is the internal "char" code, which asyncpg
        # hands back as bytes: a = NO ACTION, r = RESTRICT, c = CASCADE,
        # n = SET NULL, d = SET DEFAULT.
        assert _delete_rule(delete_rule) == "c", (
            f"{name} should cascade on delete, got {_delete_rule(delete_rule)!r}"
        )


async def test_products_are_protected_from_category_deletion(engine):
    """``products.category_id`` must RESTRICT, never CASCADE.

    CASCADE here would silently delete a seller's catalogue whenever a category
    row was removed, so the database itself has to refuse the delete and let the
    service return a 409 instead.
    """
    async with engine.connect() as conn:
        ddl = await conn.exec_driver_sql(
            "SELECT conname, confdeltype FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid = 'products'::regclass"
        )
    assert ddl, "products.category_id foreign key is missing"
    for name, delete_rule in ddl:
        assert _delete_rule(delete_rule) == "r", (
            f"{name} should RESTRICT on delete so catalogue rows cannot be "
            f"cascaded away, got {_delete_rule(delete_rule)!r}"
        )

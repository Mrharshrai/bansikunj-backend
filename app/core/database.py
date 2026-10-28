"""Async SQLAlchemy engine, session factory, and declarative base.

The engine is created lazily at import time but connections are established on
first use, so importing this module never blocks application startup. Call
:func:`check_database_connection` for readiness/liveness probes.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings

# Explicit naming convention so Alembic autogenerate produces stable,
# predictable constraint names instead of Postgres' implicit ones.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model.

    TODO Phase 2: ``User`` and ``Address`` declare ``__tablename__`` and inherit
    from this class. Alembic's ``target_metadata`` is ``Base.metadata``.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _build_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.sqlalchemy_database_uri,
        echo=settings.DB_ECHO,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        pool_pre_ping=settings.DB_POOL_PRE_PING,
        connect_args={
            "timeout": settings.DB_CONNECT_TIMEOUT,
            "server_settings": {"application_name": settings.APP_NAME},
        },
    )


engine: AsyncEngine = _build_engine()

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a session that commits on success.

    Rolls back on any exception and always closes the session.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database_connection() -> dict[str, Any]:
    """Verify the database is reachable; returns latency and server version.

    Raises:
        SQLAlchemyError: propagated so the caller can turn it into a 503.
    """
    from time import perf_counter

    started = perf_counter()
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
        version = await connection.scalar(text("SHOW server_version"))
    latency_ms = round((perf_counter() - started) * 1000, 2)
    return {
        "status": "ok",
        "dialect": engine.dialect.name,
        "server_version": version,
        "latency_ms": latency_ms,
    }


async def dispose_engine() -> None:
    """Close all pooled connections (called on application shutdown)."""
    await engine.dispose()

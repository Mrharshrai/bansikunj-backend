"""Shared pytest fixtures.

Tests run against a real PostgreSQL database (``bansikunj_test`` by default,
derived from ``DATABASE_URL`` unless ``TEST_DATABASE_URL`` is set). The schema
is built once per session by running the Alembic migrations, so the
hand-written migration is exercised on every test run.

Isolation: each test gets its own connection with an outer transaction. The
session is opened with ``join_transaction_mode="create_savepoint"``, so
endpoints can call ``commit()`` exactly as they do in production (releasing
only a SAVEPOINT) while the outer transaction is still rolled back afterwards.
Nothing survives a test.

The application's own engine is never used: ``get_db`` is overridden to yield
the test session, and the ASGI lifespan is not run, so no connection is ever
made to the development database.
"""
from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import hash_password
from app.main import app
from app.models import (  # noqa: F401
    Address,
    Banner,
    Category,
    Product,
    RefreshToken,
    Role,
    User,
)
from app.repositories.user_repository import UserRepository

#: Database the suite targets when ``TEST_DATABASE_URL`` is not set.
DEFAULT_TEST_DB_NAME = "bansikunj_test"

# --- database URL -----------------------------------------------------------

_settings = get_settings()
_explicit_test_url = os.getenv("TEST_DATABASE_URL")
if _explicit_test_url:
    # An explicit override is taken verbatim so an operator can point the suite
    # at a throwaway database on another host.
    _url = make_url(_explicit_test_url)
else:
    # Derive the test database from the app database so credentials stay in
    # one place (.env) instead of being duplicated in the environment.
    _url = make_url(_settings.sqlalchemy_database_uri).set(
        database=DEFAULT_TEST_DB_NAME
    )

TEST_DATABASE_URL = _url.render_as_string(hide_password=False)
TEST_DB_NAME = _url.database or DEFAULT_TEST_DB_NAME


# --- engine / schema --------------------------------------------------------


async def _ensure_test_database_exists() -> None:
    """``CREATE DATABASE <TEST_DB_NAME>`` if the server does not have it yet."""
    import asyncpg

    admin_url = make_url(TEST_DATABASE_URL)
    try:
        conn = await asyncpg.connect(
            host=admin_url.host or "localhost",
            port=admin_url.port or 5432,
            user=admin_url.username,
            password=admin_url.password,
            database="postgres",
        )
    except Exception:  # noqa: BLE001 - surfaced with a clearer message below
        raise RuntimeError(
            f"Cannot connect to PostgreSQL at {admin_url.host}:{admin_url.port} "
            f"as user {admin_url.username!r}. Check that the service is running "
            f"and that the password in .env is correct."
        ) from None

    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB_NAME
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator:
    """Apply the Alembic migrations once for the whole test session."""
    import asyncio

    from alembic import command
    from alembic.config import Config

    await _ensure_test_database_exists()

    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("%", "%%"))

    loop = asyncio.get_running_loop()
    # Alembic's command API is synchronous, so run it off the event loop.
    await loop.run_in_executor(None, command.upgrade, cfg, "head")

    test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, future=True)
    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    """A session bound to a connection inside a transaction rolled back per test."""
    connection = await engine.connect()
    transaction = await connection.begin()
    test_session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        yield test_session
    finally:
        await test_session.close()
        await transaction.rollback()
        await connection.close()


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """HTTP client wired to the app with the test session injected."""

    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
#  Data helpers
# ---------------------------------------------------------------------------

VALID_PASSWORD = "Str0ngPassw0rd"


async def _make_user(
    session: AsyncSession,
    *,
    email: str | None = None,
    password: str = VALID_PASSWORD,
    full_name: str = "Test User",
    phone: str = "9876543210",
    role: Role = Role.CUSTOMER,
    is_active: bool = True,
    is_email_verified: bool = True,
) -> User:
    """Insert a user directly. Flushes only, so the rollback still discards it."""
    repo = UserRepository(session)
    user = await repo.create(
        email=email or f"user-{uuid.uuid4().hex[:10]}@example.com",
        full_name=full_name,
        phone=phone,
        role=role,
        hashed_password=hash_password(password),
        is_active=is_active,
        is_email_verified=is_email_verified,
    )
    await session.flush()
    # Releases the savepoint only; the outer transaction still discards the user
    # at the end of the test.
    await session.commit()
    return user


@pytest_asyncio.fixture
async def customer(session: AsyncSession) -> User:
    """A verified, active customer."""
    return await _make_user(session)


@pytest_asyncio.fixture
async def other_customer(session: AsyncSession) -> User:
    """A second customer, used to test cross-user isolation."""
    return await _make_user(session, full_name="Other User")


@pytest_asyncio.fixture
async def admin_user(session: AsyncSession) -> User:
    return await _make_user(session, role=Role.ADMIN, full_name="Admin User")


@pytest_asyncio.fixture
async def inactive_user(session: AsyncSession) -> User:
    return await _make_user(session, is_active=False)


async def register_via_api(
    client: AsyncClient, *, email: str | None = None, password: str = VALID_PASSWORD
) -> dict:
    """Register through the API and return the parsed ``data`` payload."""
    address = email or f"api-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/api/v1/auth/register",
        json={"email": address, "password": password, "full_name": "API User"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def auth_header(client: AsyncClient, *, email: str | None = None) -> dict[str, str]:
    """Log in and return an ``Authorization`` header."""
    address = email or f"hdr-{uuid.uuid4().hex[:10]}@example.com"
    response = await client.post(
        "/api/v1/auth/login", json={"email": address, "password": VALID_PASSWORD}
    )
    assert response.status_code == 200, response.text
    token = response.json()["data"]["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def auth_header_for(
    client: AsyncClient, user: User, password: str = VALID_PASSWORD
) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": password}
    )
    assert response.status_code == 200, response.text
    token = response.json()["data"]["tokens"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
#  Catalogue helpers
# ---------------------------------------------------------------------------


async def make_category(
    session: AsyncSession,
    *,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
    is_active: bool = True,
    display_order: int = 0,
) -> Category:
    """Insert a category directly, bypassing the service's slug generation."""
    label = name or f"Category {uuid.uuid4().hex[:6]}"
    category = Category(
        name=label,
        slug=slug or f"cat-{uuid.uuid4().hex[:10]}",
        description=description,
        is_active=is_active,
        display_order=display_order,
    )
    session.add(category)
    await session.flush()
    # Release this row's savepoint. The session uses
    # ``join_transaction_mode="create_savepoint"``, so ``commit()`` only does
    # RELEASE SAVEPOINT - the outer transaction still rolls everything back at
    # the end of the test. Without this, a request that fails and rolls back
    # would also erase the fixture rows it was asserted against, because they
    # share one savepoint.
    await session.commit()
    return category


async def make_product(
    session: AsyncSession,
    category: Category | None = None,
    *,
    name: str | None = None,
    description: str = "A tasty treat.",
    price: Decimal = Decimal("199.00"),
    mrp: Decimal | None = None,
    stock_quantity: int = 10,
    is_active: bool = True,
    is_best_seller: bool = False,
    tags: list[str] | None = None,
    review_count: int = 0,
    rating: Decimal | None = None,
    image_url: str = "/assets/products/default.png",
    weight: str | None = None,
    slug: str | None = None,
) -> Product:
    """Insert a product directly."""
    category = category or await make_category(session)
    label = name or f"Product {uuid.uuid4().hex[:6]}"
    product = Product(
        category_id=category.id,
        name=label,
        slug=slug or f"prod-{uuid.uuid4().hex[:10]}",
        description=description,
        price=price,
        mrp=mrp,
        stock_quantity=stock_quantity,
        is_active=is_active,
        is_best_seller=is_best_seller,
        tags=tags or [],
        review_count=review_count,
        rating=rating,
        image_url=image_url,
        weight=weight,
    )
    session.add(product)
    await session.flush()
    # See make_category: releases the savepoint only, so a failed request cannot
    # roll back this fixture row.
    await session.commit()
    return product


async def make_banner(
    session: AsyncSession,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    image_url: str = "/assets/banners/default.png",
    link_url: str | None = None,
    is_active: bool = True,
    display_order: int = 0,
) -> Banner:
    """Insert a banner directly."""
    banner = Banner(
        title=title or f"Banner {uuid.uuid4().hex[:6]}",
        subtitle=subtitle,
        image_url=image_url,
        link_url=link_url,
        is_active=is_active,
        display_order=display_order,
    )
    session.add(banner)
    await session.flush()
    # Releases this row's savepoint only; the outer transaction still rolls the
    # banner back at the end of the test (see make_category).
    await session.commit()
    return banner


@pytest_asyncio.fixture
async def category(session: AsyncSession) -> Category:
    return await make_category(session, name="Soft Chewy", slug="soft-chewy")


@pytest_asyncio.fixture
async def admin_headers(client: AsyncClient, session: AsyncSession) -> dict[str, str]:
    """Authorization header for a freshly created admin."""
    admin = await _make_user(session, role=Role.ADMIN, full_name="Catalogue Admin")
    return await auth_header_for(client, admin)


@pytest_asyncio.fixture
async def customer_headers(client: AsyncClient, session: AsyncSession) -> dict[str, str]:
    """Authorization header for an ordinary customer.

    A user must exist before login can succeed, so create one and log in as
    them. Calling ``auth_header`` here would invent a random address for a
    user that was never registered, and every request would be a 401.
    """
    user = await _make_user(session)
    return await auth_header_for(client, user)


def product_payload(category_slug: str, **overrides) -> dict:
    """A valid admin create-product body."""
    body = {
        "name": "Belgian Dark Chunk",
        "description": "70% dark chocolate chunks in a soft chewy cookie.",
        "category": category_slug,
        "price": "249.00",
        "mrp": "299.00",
        "stock_quantity": 25,
        "image_url": "/assets/products/belgian-dark.jpg",
        "weight": "12 cookies - 480g",
        "tags": ["chocolate", "belgian"],
    }
    body.update(overrides)
    return body

"""Stock adjustments must not lose a concurrent write.

The rest of the suite runs every request through one session inside a rolled-back
transaction, so it cannot express two genuinely concurrent writers. These tests
open independent, committed connections against the test database instead, which
is the only way to show that the ``SELECT ... FOR UPDATE`` in
``ProductRepository.get_locked_by_id_or_slug`` actually serialises adjustments.

The last test is a deliberate control: it performs the same read-modify-write
*without* the lock and asserts the update IS lost. Without it, the first test
could pass simply because the interleaving never happened to collide.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.product import Product
from app.repositories.product_repository import ProductRepository
from app.schemas.product import StockAdjustment
from app.services.product_service import ProductService
from tests.conftest import make_category, make_product

pytestmark = pytest.mark.asyncio

#: Concurrent writers. High enough that a collision is near-certain, low enough
#: that the test stays fast.
WRITERS = 8

#: Units added by each writer.
STEP = 5

START_STOCK = 100


@pytest_asyncio.fixture
async def live_product(engine):
    """A *committed* product, visible to independent connections.

    Deliberately not built on the ``session`` fixture: that one holds an
    uncommitted outer transaction, so a second connection would not see the row
    at all.
    """
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        category = await make_category(session)
        product = await make_product(
            session, category, name="Concurrency Cookie", stock_quantity=START_STOCK
        )
        await session.commit()
        product_id, category_id = product.id, category.id

    try:
        yield product_id
    finally:
        async with AsyncSession(bind=engine) as session:
            await session.execute(delete(Product).where(Product.id == product_id))
            await session.execute(delete(Category).where(Category.id == category_id))
            await session.commit()


async def _adjust_once(engine, product_id, delta):
    """One admin adjustment on its own connection, committed before returning."""
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        service = ProductService(session)
        _, _, new = await service.adjust_stock(
            product_id, StockAdjustment(delta=delta, reason="concurrency probe")
        )
        await session.commit()
        return new


async def _adjust_unlocked_once(engine, product_id, delta):
    """The same change, minus the row lock -- the bug the lock prevents."""
    async with AsyncSession(bind=engine, expire_on_commit=False) as session:
        product = await ProductRepository(session).get_by_id(product_id)
        product.stock_quantity += delta
        await session.flush()
        await session.commit()
        return product.stock_quantity


async def _read_stock(engine, product_id) -> int:
    async with AsyncSession(bind=engine) as session:
        result = await session.execute(
            select(Product.stock_quantity).where(Product.id == product_id)
        )
        return int(result.scalar_one())


async def test_locked_adjustments_do_not_lose_updates(engine, live_product):
    """Every writer's change must survive, not just the last one to commit."""
    await asyncio.gather(*(_adjust_once(engine, live_product, STEP) for _ in range(WRITERS)))
    assert await _read_stock(engine, live_product) == START_STOCK + WRITERS * STEP


async def test_lock_survives_a_mix_of_gains_and_losses(engine, live_product):
    """Deltas in both directions, still no lost updates."""
    deltas = [10, -5, 20, -1, 7, -30, 3, 15]
    await asyncio.gather(*(_adjust_once(engine, live_product, d) for d in deltas))
    assert await _read_stock(engine, live_product) == START_STOCK + sum(deltas)


async def test_unlocked_writes_would_lose_updates(engine, live_product):
    """Control: proves the assertion above is actually sensitive to a race.

    Without ``FOR UPDATE`` each writer reads the same starting quantity and then
    overwrites its neighbours, so the total ends up short. If this test ever
    passes, the fixture is not producing real concurrency and the lock test is
    proving nothing.
    """
    await asyncio.gather(
        *(_adjust_unlocked_once(engine, live_product, STEP) for _ in range(WRITERS))
    )
    final = await _read_stock(engine, live_product)
    assert final < START_STOCK + WRITERS * STEP, (
        f"unlocked read-modify-write lost {START_STOCK + WRITERS * STEP - final} updates; "
        "the concurrency fixture is not interleaving and the lock tests are vacuous"
    )

"""Order persistence.

Two things live here that the rest of the codebase should not have to think about:

* **Owner scoping.** Every read is filtered by ``user_id`` in the ``WHERE``
  clause, so another customer's order is indistinguishable from one that does
  not exist. The order id is a UUID, but obscurity is not a security control.
* **Row locks for stock.** :meth:`OrderRepository.lock_products` issues
  ``SELECT ... FOR UPDATE``. Two simultaneous checkouts of the last cookie must
  serialise on the product row rather than both reading ``stock_quantity = 1``,
  decrementing it, and committing an oversell. A plain ``SELECT`` gives no such
  guarantee, no matter how the arithmetic is written afterwards.
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models.order import (
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
)
from ..models.product import Product


def _item_options():
    """Eager-load an order's lines and their products, without N+1 queries."""
    return (
        selectinload(Order.items).selectinload(OrderItem.product).selectinload(Product.category),
    )


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ write
    async def create(self, order: Order) -> Order:
        self.session.add(order)
        await self.session.flush()
        return order

    async def reload(self, order: Order) -> Order:
        """Re-read an order with its lines and products loaded.

        Called after placing an order, because the ``items`` relationship was
        populated by assignment rather than by a query and the caller still
        needs it rendered.
        """
        result = await self.session.execute(
            select(Order).options(*_item_options()).where(Order.id == order.id)
        )
        return result.scalar_one()

    async def set_status(
        self, order: Order, status: OrderStatus, *, changed_by: uuid.UUID | None = None, note: str | None = None
    ) -> Order:
        # Capture the status before it is overwritten: the audit trail has to
        # record where the order came from, otherwise every row reads
        # ``from_status = NULL`` and the previous state is unrecoverable.
        previous = order.status
        order.status = status
        if status is OrderStatus.CANCELLED:
            order.cancelled_at = func.now()
        elif status is OrderStatus.DELIVERED:
            order.delivered_at = func.now()
        await self.session.flush()
        await self.record_status(
            order,
            to_status=status,
            from_status=previous if previous is not status else None,
            changed_by=changed_by,
            note=note,
        )
        return order

    async def record_status(
        self,
        order: Order,
        *,
        to_status: OrderStatus,
        from_status: OrderStatus | None = None,
        changed_by: uuid.UUID | None = None,
        note: str | None = None,
    ) -> OrderStatusHistory:
        entry = OrderStatusHistory(
            order_id=order.id,
            from_status=from_status,
            to_status=to_status,
            changed_by_id=changed_by,
            note=note,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    # ------------------------------------------------------------------- read
    async def get_by_id(
        self, user_id: uuid.UUID | str, order_id: uuid.UUID | str
    ) -> Order | None:
        """Fetch one order **scoped to its owner**, fully loaded."""
        result = await self.session.execute(
            select(Order)
            .options(*_item_options())
            .where(
                Order.id == self._as_uuid(order_id),
                Order.user_id == self._as_uuid(user_id),
            )
        )
        return result.scalar_one_or_none()

    async def get_any_by_id(self, order_id: uuid.UUID | str) -> Order | None:
        """Fetch one order with no owner filter.

        For administrative reads only. Customer-facing handlers must use
        :meth:`get_by_id`, which scopes to the caller.
        """
        result = await self.session.execute(
            select(Order).options(*_item_options()).where(Order.id == self._as_uuid(order_id))
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self, user_id: uuid.UUID | str, *, limit: int = 20, offset: int = 0
    ) -> list[Order]:
        """A user's orders, newest first."""
        result = await self.session.execute(
            select(Order)
            .options(*_item_options())
            .where(Order.user_id == self._as_uuid(user_id))
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_user(self, user_id: uuid.UUID | str) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(Order).where(Order.user_id == self._as_uuid(user_id))
        )
        return int(result.scalar_one())

    async def status_history(self, order_id: uuid.UUID | str) -> list[OrderStatusHistory]:
        result = await self.session.execute(
            select(OrderStatusHistory)
            .where(OrderStatusHistory.order_id == self._as_uuid(order_id))
            .order_by(OrderStatusHistory.created_at.asc(), OrderStatusHistory.id.asc())
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------ stock
    async def lock_products(self, product_ids: list[uuid.UUID]) -> dict[uuid.UUID, Product]:
        """Lock the given product rows for the rest of the transaction.

        Returns them keyed by id. Rows are locked in a deterministic (sorted)
        order so two concurrent orders touching the same two products cannot
        deadlock by grabbing them in opposite orders.

        The lock is held until the transaction commits or rolls back, which is
        the whole point: it makes the read-then-decrement below atomic against
        other writers.
        """
        if not product_ids:
            return {}
        result = await self.session.execute(
            select(Product)
            .where(Product.id.in_(sorted(product_ids, key=str)))
            .order_by(Product.id)
            .with_for_update()
        )
        return {product.id: product for product in result.scalars().all()}

    @staticmethod
    def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


__all__ = ["OrderRepository"]

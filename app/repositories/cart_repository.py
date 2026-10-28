"""Cart persistence. Every query is scoped to the owning ``user_id``.

Scoping happens inside the WHERE clause rather than by filtering afterwards, so
another user's cart line is indistinguishable from one that does not exist --
which is what stops an enumeration attack from confirming that some other
customer has a given product in their basket.
"""
from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models.cart_item import CartItem
from ..models.product import Product

#: The response embeds a ``ProductOut``, whose ``from_entity`` reads
#: ``product.category``. That read must never become a lazy load in an async
#: context (it raises MissingGreenlet and surfaces as a 503), so the category is
#: chained on explicitly rather than relying on the mapper's ``selectin``.
_PRODUCT_WITH_CATEGORY = selectinload(CartItem.product).selectinload(Product.category)


class CartRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self, user_id: uuid.UUID, product_id: uuid.UUID, *, quantity: int
    ) -> CartItem:
        item = CartItem(user_id=user_id, product_id=product_id, quantity=quantity)
        self.session.add(item)
        await self.session.flush()
        return await self.get_by_product_id(user_id, product_id)

    async def get_by_product_id(
        self, user_id: uuid.UUID | str, product_id: uuid.UUID | str
    ) -> CartItem | None:
        """Fetch one line *scoped to its owner*, with the product loaded."""
        result = await self.session.execute(
            select(CartItem)
            .options(_PRODUCT_WITH_CATEGORY)
            .where(
                CartItem.user_id == self._as_uuid(user_id),
                CartItem.product_id == self._as_uuid(product_id),
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self, user_id: uuid.UUID | str, *, limit: int = 100, offset: int = 0
    ) -> list[CartItem]:
        """Every line for one user, oldest first so the order is stable."""
        result = await self.session.execute(
            select(CartItem)
            .options(_PRODUCT_WITH_CATEGORY)
            .where(CartItem.user_id == self._as_uuid(user_id))
            .order_by(CartItem.created_at.asc(), CartItem.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def set_quantity(
        self, item: CartItem, quantity: int
    ) -> CartItem:
        item.quantity = quantity
        await self.session.flush()
        return item

    async def delete_by_product_id(
        self, user_id: uuid.UUID | str, product_id: uuid.UUID | str
    ) -> bool:
        """Remove one line. Returns whether a row was actually deleted."""
        result = await self.session.execute(
            delete(CartItem).where(
                CartItem.user_id == self._as_uuid(user_id),
                CartItem.product_id == self._as_uuid(product_id),
            )
        )
        return bool(result.rowcount)

    async def clear(self, user_id: uuid.UUID | str) -> int:
        """Empty the whole cart. Returns the number of lines removed."""
        result = await self.session.execute(
            delete(CartItem).where(CartItem.user_id == self._as_uuid(user_id))
        )
        return int(result.rowcount or 0)

    async def count_for_user(self, user_id: uuid.UUID | str) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(CartItem)
            .where(CartItem.user_id == self._as_uuid(user_id))
        )
        return int(result.scalar_one())

    @staticmethod
    def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

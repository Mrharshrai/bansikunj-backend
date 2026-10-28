"""Cart business logic.

Two rules live here and nowhere else:

* **The client never sets a price.** Lines are priced from the catalogue row, so
  a modified request body cannot change what a customer is charged.
* **Stock is checked against the resulting line quantity**, not against the
  quantity in the request. Adding a product that is already in the cart
  increments, so the sum is what has to fit in stock.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import ConflictError, NotFoundError, ValidationError
from ..core.logging import get_logger
from ..models.cart_item import CartItem
from ..models.product import Product
from ..repositories.cart_repository import CartRepository
from ..repositories.product_repository import ProductRepository
from ..schemas.cart import MAX_CART_QUANTITY, CartItemAdd, CartItemQuantityUpdate

logger = get_logger(__name__)


class CartService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.carts = CartRepository(session)
        self.products = ProductRepository(session)

    # ------------------------------------------------------------------ read
    async def list_items(self, user_id: uuid.UUID) -> list[CartItem]:
        return await self.carts.list_for_user(user_id)

    # ----------------------------------------------------------------- write
    async def add(self, user_id: uuid.UUID, payload: CartItemAdd) -> list[CartItem]:
        """Add a product, incrementing the quantity if it is already present."""
        product = await self._purchasable_product(payload.product_id)

        existing = await self.carts.get_by_product_id(user_id, payload.product_id)
        if existing is None:
            self._assert_stock(product, payload.quantity)
            await self.carts.create(
                user_id, product.id, quantity=payload.quantity
            )
            logger.info(
                "cart.item_added",
                user_id=str(user_id),
                product_id=str(product.id),
                quantity=payload.quantity,
            )
        else:
            total = existing.quantity + payload.quantity
            self._assert_within_cap(total)
            self._assert_stock(product, total)
            await self.carts.set_quantity(existing, total)
            logger.info(
                "cart.item_incremented",
                user_id=str(user_id),
                product_id=str(product.id),
                quantity=total,
            )

        return await self.list_items(user_id)

    async def set_quantity(
        self, user_id: uuid.UUID, product_id: uuid.UUID | str, payload: CartItemQuantityUpdate
    ) -> list[CartItem]:
        """Replace a line's quantity. Refuses a line that is not in the cart."""
        product = await self._purchasable_product(product_id)
        item = await self.carts.get_by_product_id(user_id, product_id)
        if item is None:
            raise NotFoundError(
                "That product is not in your cart", error_code="cart_item_not_found"
            )
        self._assert_stock(product, payload.quantity)
        await self.carts.set_quantity(item, payload.quantity)
        logger.info(
            "cart.item_quantity_set",
            user_id=str(user_id),
            product_id=str(product.id),
            quantity=payload.quantity,
        )
        return await self.list_items(user_id)

    async def remove(
        self, user_id: uuid.UUID, product_id: uuid.UUID | str
    ) -> list[CartItem]:
        """Remove one line.

        Idempotent: removing something that is not there is a success, because
        the caller's intent -- "this should not be in my cart" -- already holds.
        """
        removed = await self.carts.delete_by_product_id(user_id, product_id)
        if removed:
            logger.info(
                "cart.item_removed",
                user_id=str(user_id),
                product_id=str(product_id),
            )
        return await self.list_items(user_id)

    async def clear(self, user_id: uuid.UUID) -> list[CartItem]:
        removed = await self.carts.clear(user_id)
        logger.info("cart.cleared", user_id=str(user_id), lines=removed)
        return []

    # --------------------------------------------------------------- internal
    async def _purchasable_product(
        self, product_id: uuid.UUID | str
    ) -> Product:
        """Load a product that a customer is allowed to put in a cart.

        A delisted product is reported as missing rather than as forbidden, so
        the endpoint cannot be used to discover products that exist but are not
        for sale.
        """
        product = await self.products.get_by_id_or_slug(
            product_id, include_inactive=False
        )
        if product is None or not product.is_active:
            raise NotFoundError("Product not found", error_code="product_not_found")
        return product

    @staticmethod
    def _assert_stock(product: Product, quantity: int) -> None:
        if quantity > product.stock_quantity:
            raise ConflictError(
                "Not enough stock for that quantity",
                error_code="insufficient_stock",
                details={
                    "requested": quantity,
                    "available": product.stock_quantity,
                    "product_id": str(product.id),
                },
            )

    @staticmethod
    def _assert_within_cap(quantity: int) -> None:
        if quantity > MAX_CART_QUANTITY:
            raise ValidationError(
                f"A single line may hold at most {MAX_CART_QUANTITY} items",
                error_code="cart_quantity_exceeded",
                details={"maximum": MAX_CART_QUANTITY, "requested": quantity},
            )


__all__ = ["CartService"]

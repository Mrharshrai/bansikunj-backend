"""Cart request/response schemas.

The client never sends a price. It sends ``product_id`` and ``quantity`` only, and
the server prices the line from the catalogue, so a tampered or stale client
cannot dictate what something costs.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from .product import ProductOut

#: Upper bound on a single line. A guard against a client asking for 10_000 of
#: something; the real limit is always available stock.
MAX_CART_QUANTITY = 99


class CartItemAdd(BaseModel):
    """Add a product to the cart.

    If the product is already in the cart the quantity is *incremented* rather
    than a second line being created, so adding the same product twice never
    produces duplicate rows.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    product_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=MAX_CART_QUANTITY)


class CartItemQuantityUpdate(BaseModel):
    """Replace a line's quantity.

    Quantity must be at least 1: removing a line is what :class:`CartItemRemove`
    and the ``DELETE`` endpoint are for, so a single endpoint has one job.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    quantity: int = Field(ge=1, le=MAX_CART_QUANTITY)


class CartItemOut(BaseModel):
    """One cart line, with the product embedded and the line priced server-side.

    This is a superset of the client's ``CartItem`` (``productId``, ``quantity``),
    so a client written against the smaller shape keeps working while a client
    that wants to render the line does not need a second request per item.
    """

    model_config = ConfigDict(from_attributes=True)

    product_id: uuid.UUID
    quantity: int
    line_total: Decimal
    product: ProductOut

    @classmethod
    def from_entity(cls, item) -> "CartItemOut":
        return cls(
            product_id=item.product_id,
            quantity=item.quantity,
            line_total=item.line_total,
            product=ProductOut.from_entity(item.product),
        )


class CartOut(BaseModel):
    """The whole cart plus its totals.

    Every cart endpoint returns this rather than a single line, so a client that
    has just mutated the cart never has to follow up with a read to learn the
    new state.
    """

    items: list[CartItemOut] = Field(default_factory=list)

    #: Number of distinct products (i.e. rows), not the sum of quantities.
    line_count: int = 0
    #: Sum of quantities across every line.
    item_count: int = 0
    subtotal: Decimal = Decimal("0.00")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_empty(self) -> bool:
        return not self.items

    @classmethod
    def from_entities(cls, rows) -> "CartOut":
        """Build the response from a list of ``CartItem`` rows.

        ``subtotal`` is the sum of the line totals. Delivery charge and any
        discount are deliberately *not* included: they depend on the delivery
        address and on promotions, both of which belong to order placement, not
        to a cart. The client must not present a "total" that the server has
        not computed.
        """
        items = [CartItemOut.from_entity(row) for row in rows]
        return cls(
            items=items,
            line_count=len(items),
            item_count=sum(item.quantity for item in items),
            subtotal=sum((item.line_total for item in items), Decimal("0.00")),
        )


__all__ = [
    "MAX_CART_QUANTITY",
    "CartItemAdd",
    "CartItemOut",
    "CartItemQuantityUpdate",
    "CartOut",
]

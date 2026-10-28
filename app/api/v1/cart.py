"""Cart endpoints.

Every route requires an authenticated caller: a cart is private to its owner,
so there is no public or admin variant. Role checks are irrelevant here -- any
signed-in shopper may manage their own basket, and may only ever touch their own.

Each endpoint returns the *whole* cart, not just the affected line, so a client
that has just mutated the basket never needs a follow-up read to learn the new
state. The Angular client ignores these bodies and rebuilds from localStorage,
so returning a superset of its ``CartItem`` shape is safe.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from ...core.dependencies import CurrentUser, DbSessionDep
from ...core.envelope import SuccessEnvelope
from ...models.user import User
from ...schemas.cart import CartItemAdd, CartItemQuantityUpdate, CartOut
from ...services.cart_service import CartService

router = APIRouter(prefix="/cart", tags=["cart"])


def get_cart_service(session: DbSessionDep) -> CartService:
    return CartService(session)


CartServiceDep = Annotated[CartService, Depends(get_cart_service)]


@router.get("/items", response_model=SuccessEnvelope[CartOut], summary="View the cart")
async def list_cart_items(
    service: CartServiceDep, user: CurrentUser
) -> SuccessEnvelope[CartOut]:
    """The caller's cart.

    A user with no cart lines gets an empty cart rather than a 404: no rows *is*
    the empty cart.
    """
    rows = await service.list_items(user.id)
    return SuccessEnvelope[CartOut](
        message="Cart retrieved successfully", data=CartOut.from_entities(rows)
    )


@router.post(
    "/items",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[CartOut],
    summary="Add a product to the cart",
)
async def add_cart_item(
    payload: CartItemAdd, service: CartServiceDep, user: CurrentUser
) -> SuccessEnvelope[CartOut]:
    """Add a product, or increment it if it is already in the cart.

    Only ``product_id`` and ``quantity`` are accepted; the price always comes
    from the catalogue. Adding a delisted product is a 404, and asking for more
    than is in stock is a 409.
    """
    rows = await service.add(user.id, payload)
    return SuccessEnvelope[CartOut](
        message="Product added to cart", data=CartOut.from_entities(rows)
    )


@router.put(
    "/items/{product_id}",
    response_model=SuccessEnvelope[CartOut],
    summary="Set a line's quantity",
)
async def set_cart_item_quantity(
    product_id: uuid.UUID,
    payload: CartItemQuantityUpdate,
    service: CartServiceDep,
    user: CurrentUser,
) -> SuccessEnvelope[CartOut]:
    """Replace the quantity of one line.

    The quantity is absolute, not a delta. Use ``DELETE`` to remove a line --
    a quantity of 0 is rejected rather than silently deleting, so each endpoint
    has exactly one job.
    """
    rows = await service.set_quantity(user.id, product_id, payload)
    return SuccessEnvelope[CartOut](
        message="Cart updated successfully", data=CartOut.from_entities(rows)
    )


@router.delete(
    "/items/{product_id}",
    response_model=SuccessEnvelope[CartOut],
    summary="Remove a line",
)
async def remove_cart_item(
    product_id: uuid.UUID, service: CartServiceDep, user: CurrentUser
) -> SuccessEnvelope[CartOut]:
    """Remove one line. Succeeds even if the line was already gone."""
    rows = await service.remove(user.id, product_id)
    return SuccessEnvelope[CartOut](
        message="Product removed from cart", data=CartOut.from_entities(rows)
    )


@router.delete("/items", response_model=SuccessEnvelope[CartOut], summary="Empty the cart")
async def clear_cart(
    service: CartServiceDep, user: CurrentUser
) -> SuccessEnvelope[CartOut]:
    """Remove every line from the caller's cart."""
    rows = await service.clear(user.id)
    return SuccessEnvelope[CartOut](
        message="Cart cleared successfully", data=CartOut.from_entities(rows)
    )

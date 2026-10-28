"""Order endpoints.

Every route is owner-scoped: a caller sees only their own orders, and another
customer's order id is a 404 rather than a 403 so the endpoint cannot be used to
confirm that an order exists.

The three routes are placement, listing, and cancellation. Administrative status
transitions (confirm, ship, deliver) belong to Phase 8, but the transition rules
they will use already live in :class:`~app.services.order_service.OrderService`
so that they cannot be implemented inconsistently.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from ...core.dependencies import CurrentUser, DbSessionDep
from ...core.envelope import SuccessEnvelope
from ...models.user import User
from ...schemas.order import OrderCreate, OrderOut, OrderPage
from ...services.order_service import OrderService

router = APIRouter(prefix="/orders", tags=["orders"])


def get_order_service(session: DbSessionDep) -> OrderService:
    return OrderService(session)


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[OrderOut],
    summary="Place an order",
)
async def place_order(
    payload: OrderCreate, service: OrderServiceDep, user: CurrentUser
) -> SuccessEnvelope[OrderOut]:
    """Place an order, decrementing stock and consuming the purchased lines.

    The body carries no prices. Send ``items`` to buy an explicit list, or omit
    it to buy the whole server cart. Either way every amount is computed from
    the catalogue, and the request cannot influence what is charged.
    """
    order = await service.place(user, payload)
    return SuccessEnvelope[OrderOut](
        message="Order placed successfully", data=OrderOut.from_entity(order)
    )


@router.get("", response_model=SuccessEnvelope[OrderPage], summary="List my orders")
async def list_orders(
    service: OrderServiceDep,
    user: CurrentUser,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SuccessEnvelope[OrderPage]:
    """The caller's orders, newest first. Never another customer's."""
    rows, total = await service.list_for_user(user.id, page=page, limit=limit)
    return SuccessEnvelope[OrderPage](
        message="Orders retrieved successfully",
        data=OrderPage.build(
            [OrderOut.from_entity(row) for row in rows], total=total, page=page, limit=limit
        ),
    )


@router.get("/{order_id}", response_model=SuccessEnvelope[OrderOut], summary="Get an order")
async def get_order(
    order_id: Annotated[uuid.UUID, Path(description="Order id")],
    service: OrderServiceDep,
    user: CurrentUser,
) -> SuccessEnvelope[OrderOut]:
    """One of the caller's own orders."""
    order = await service.get(user.id, order_id)
    return SuccessEnvelope[OrderOut](
        message="Order retrieved successfully", data=OrderOut.from_entity(order)
    )


@router.post(
    "/{order_id}/cancel",
    response_model=SuccessEnvelope[OrderOut],
    summary="Cancel an order",
)
async def cancel_order(
    order_id: Annotated[uuid.UUID, Path(description="Order id")],
    service: OrderServiceDep,
    user: CurrentUser,
) -> SuccessEnvelope[OrderOut]:
    """Cancel an order and return its stock to the catalogue.

    Allowed until the order ships. Cancelling an already-cancelled, delivered,
    or otherwise terminal order is a 409 rather than a silent success, because
    a silent success would hide a double-click that also released stock.
    """
    order = await service.cancel(user, order_id)
    return SuccessEnvelope[OrderOut](
        message="Order cancelled successfully", data=OrderOut.from_entity(order)
    )


__all__ = ["router"]

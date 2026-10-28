"""Order business logic.

Placing an order is the one operation in this shop that moves money, so the
rules are all here rather than spread across the endpoint and the model:

* **Nothing about an order is priced by the client.** The request carries
  product ids and quantities; every price comes from the catalogue row, and the
  delivery charge comes from configuration.
* **Stock is decremented under a row lock.** The product rows are locked
  ``FOR UPDATE`` before availability is checked, so two people checking out the
  last item cannot both succeed.
* **An order is a snapshot, a cart is a view.** Cart lines price live and change
  when the catalogue does; order lines store the price that was charged. That
  asymmetry is deliberate and is why ``OrderItem`` carries ``unit_price``.
* **Cancellation returns the stock**, exactly once, and only from a status that
  was still holding it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..core.exceptions import ConflictError, NotFoundError, ValidationError
from ..core.logging import get_logger
from ..models.order import (
    ALLOWED_TRANSITIONS,
    CANCELLABLE_STATUSES,
    STOCK_HELD_STATUSES,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
)
from ..models.payment import PaymentStatus
from ..models.product import Product
from ..models.user import User
from ..repositories.cart_repository import CartRepository
from ..repositories.order_repository import OrderRepository
from ..schemas.order import OrderCreate

logger = get_logger(__name__)


@dataclass(frozen=True)
class OrderTotals:
    """The computed money on an order.

    A frozen dataclass rather than a dict so the arithmetic can be unit-tested
    without a database, and so nothing downstream can mutate a total that has
    already been checked against the database constraints.
    """

    subtotal: Decimal
    delivery_charge: Decimal
    discount: Decimal
    total: Decimal
    free_delivery_applied: bool


def compute_totals(
    subtotal: Decimal,
    *,
    delivery_charge: Decimal,
    free_delivery_threshold: Decimal,
    discount: Decimal = Decimal("0.00"),
) -> OrderTotals:
    """Turn a subtotal into the four numbers an order is stored with.

    Delivery is free at or above ``free_delivery_threshold``. A threshold of
    zero means "never free" (every order pays the charge), which is the escape
    hatch for turning the promotion off without a code change.

    The arithmetic is ``Decimal`` throughout. Prices are ``NUMERIC(12,2)`` in
    the database, and float money would eventually produce a total that differs
    from the sum of its own line totals.
    """
    subtotal = subtotal.quantize(Decimal("0.01"))
    qualifies = free_delivery_threshold > 0 and subtotal >= free_delivery_threshold
    charge = Decimal("0.00") if qualifies else delivery_charge.quantize(Decimal("0.01"))
    total = subtotal + charge - discount
    if total < 0:
        # A discount larger than the order would otherwise persist a negative
        # total, which the `total >= 0` constraint would reject with an opaque
        # database error. Clamp here so the failure is legible.
        total = Decimal("0.00")
    return OrderTotals(
        subtotal=subtotal,
        delivery_charge=charge,
        discount=discount,
        total=total.quantize(Decimal("0.01")),
        free_delivery_applied=qualifies,
    )


@dataclass(frozen=True)
class _RequestedLine:
    product_id: uuid.UUID
    quantity: int


class OrderService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.orders = OrderRepository(session)
        self.carts = CartRepository(session)
        self.settings = settings or get_settings()

    # -------------------------------------------------------------------- read
    async def list_for_user(
        self, user_id: uuid.UUID, *, page: int = 1, limit: int = 20
    ) -> tuple[list[Order], int]:
        total = await self.orders.count_for_user(user_id)
        rows = await self.orders.list_for_user(user_id, limit=limit, offset=(page - 1) * limit)
        return rows, total

    async def get(self, user_id: uuid.UUID, order_id: uuid.UUID) -> Order:
        """One of the caller's own orders.

        Scoped by owner, so another customer's order id is a 404 rather than a
        403 -- a 403 would confirm that the order exists.
        """
        order = await self.orders.get_by_id(user_id, order_id)
        if order is None:
            raise NotFoundError("Order not found", error_code="order_not_found")
        return order

    # ------------------------------------------------------------------- write
    async def place(self, user: User, payload: OrderCreate) -> Order:
        """Create an order, decrement stock, and consume the purchased lines.

        Everything happens in the caller's transaction, which ``get_db``
        commits only if this returns without raising. A failure part-way
        through therefore leaves neither an order nor a decremented stock
        level -- there is no window in which one exists without the other.
        """
        lines = await self._resolve_lines(user.id, payload)
        self._assert_line_limits(lines)

        products = await self._lock_purchasable_products(lines)
        priced = self._price_lines(lines, products)

        subtotal = sum((line.unit_price * line.quantity for line in priced), Decimal("0.00"))
        totals = compute_totals(
            subtotal,
            delivery_charge=self.settings.DELIVERY_CHARGE,
            free_delivery_threshold=self.settings.FREE_DELIVERY_THRESHOLD,
        )

        order = Order(
            user_id=user.id,
            status=OrderStatus.PENDING,
            payment_method=payload.payment_method,
            payment_status=(
                PaymentStatus.NOT_REQUIRED
                if payload.payment_method is PaymentMethod.COD
                else PaymentStatus.NOT_STARTED
            ),
            customer_name=payload.name,
            customer_email=str(payload.email),
            customer_phone=payload.phone,
            address_line1=payload.address.line1,
            address_line2=payload.address.line2,
            city=payload.address.city,
            state=payload.address.state,
            pincode=payload.address.pincode,
            country=payload.address.country,
            subtotal=totals.subtotal,
            delivery_charge=totals.delivery_charge,
            discount=totals.discount,
            total=totals.total,
        )
        for line in priced:
            order.items.append(
                OrderItem(
                    product_id=line.product_id,
                    # Snapshots: what was charged, not what the catalogue says
                    # today.
                    product_name=line.product_name,
                    unit_price=line.unit_price,
                    quantity=line.quantity,
                    line_total=(line.unit_price * line.quantity),
                )
            )
            products[line.product_id].stock_quantity -= line.quantity

        await self.orders.create(order)
        await self.orders.record_status(
            order,
            to_status=OrderStatus.PENDING,
            changed_by=user.id,
            note="Order placed",
        )
        await self._consume_cart_lines(user.id, [line.product_id for line in priced])

        logger.info(
            "order.placed",
            order_id=str(order.id),
            user_id=str(user.id),
            lines=len(priced),
            items=sum(line.quantity for line in priced),
            subtotal=str(totals.subtotal),
            total=str(totals.total),
            payment_method=payload.payment_method.value,
        )
        return await self.orders.reload(order)

    async def cancel(
        self, user: User | uuid.UUID, order_id: uuid.UUID
    ) -> Order:
        """Cancel one of the caller's orders and return its stock.

        Stock is only returned if the order was still holding it, so cancelling
        an already-cancelled order cannot inflate the catalogue -- the status
        check happens first and a terminal status is a 409.

        A captured online payment is a 409 as well: the money has to be refunded
        first (see ``PaymentService.refund``), and that path re-enters here
        after the provider has accepted the refund.
        """
        user_id = user.id if isinstance(user, User) else user
        order = await self.get(user_id, order_id)
        if order.payment_status is PaymentStatus.PAID:
            raise ConflictError(
                "A paid order must be refunded before it can be cancelled",
                error_code="paid_order_requires_refund",
            )
        if order.status not in CANCELLABLE_STATUSES:
            raise ConflictError(
                f"An order that is {order.status.value} can no longer be cancelled",
                error_code="order_not_cancellable",
                details={"status": order.status.value},
            )
        self.assert_transition(order.status, OrderStatus.CANCELLED)

        if order.status in STOCK_HELD_STATUSES:
            await self._release_stock(order)

        previous = order.status
        await self.orders.set_status(
            order, OrderStatus.CANCELLED, changed_by=user_id, note="Cancelled by customer"
        )
        logger.info(
            "order.cancelled",
            order_id=str(order.id),
            user_id=str(user_id),
            previous_status=previous.value,
        )
        return await self.orders.reload(order)

    async def advance(
        self,
        order: Order,
        target: OrderStatus,
        *,
        changed_by: uuid.UUID | None = None,
        note: str | None = None,
    ) -> Order:
        """Move an order to ``target``, validating the transition.

        Used by the customer cancellation path and, from Phase 8, by the admin
        status endpoint. The transition table is enforced here rather than in
        the endpoint so that no caller can bypass it.
        """
        self.assert_transition(order.status, target)
        previous = order.status
        await self.orders.set_status(order, target, changed_by=changed_by, note=note)
        logger.info(
            "order.status_changed",
            order_id=str(order.id),
            previous_status=previous.value,
            status=target.value,
        )
        return await self.orders.reload(order)

    # ------------------------------------------------------------ status rules
    @staticmethod
    def assert_transition(current: OrderStatus, target: OrderStatus) -> None:
        """Reject an illegal status move with a 409.

        Terminal states (delivered, cancelled) have no outgoing transitions, and
        an order can never move backwards.
        """
        if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise ConflictError(
                f"An order cannot move from {current.value} to {target.value}",
                error_code="invalid_status_transition",
                details={
                    "current_status": current.value,
                    "requested_status": target.value,
                    "allowed": sorted(
                        status.value for status in ALLOWED_TRANSITIONS.get(current, frozenset())
                    ),
                },
            )

    # --------------------------------------------------------------- internals
    async def _resolve_lines(
        self, user_id: uuid.UUID, payload: OrderCreate
    ) -> list[_RequestedLine]:
        """Decide which lines to buy: the request's, or the server cart's.

        When the client sends ``items`` it is the list to buy -- the frontend
        treats ``localStorage`` as authoritative, so the request and the server
        cart can legitimately differ. Either way the *prices* come from the
        catalogue, not from whichever list was chosen.
        """
        if payload.items is None:
            cart_items = await self.carts.list_for_user(user_id)
            if not cart_items:
                raise ConflictError(
                    "Your cart is empty", error_code="cart_empty"
                )
            return [
                _RequestedLine(product_id=item.product_id, quantity=item.quantity)
                for item in cart_items
            ]
        return [
            _RequestedLine(product_id=line.product_id, quantity=line.quantity)
            for line in payload.items
        ]

    def _assert_line_limits(self, lines: list[_RequestedLine]) -> None:
        """Bound the size of an order before any row is written."""
        if len(lines) > self.settings.MAX_ORDER_LINES:
            raise ValidationError(
                f"An order may contain at most {self.settings.MAX_ORDER_LINES} distinct products",
                error_code="order_too_many_lines",
                details={"lines": len(lines), "maximum": self.settings.MAX_ORDER_LINES},
            )
        total_items = sum(line.quantity for line in lines)
        if total_items > self.settings.MAX_ORDER_ITEMS:
            raise ValidationError(
                f"An order may contain at most {self.settings.MAX_ORDER_ITEMS} items",
                error_code="order_too_many_items",
                details={"items": total_items, "maximum": self.settings.MAX_ORDER_ITEMS},
            )

    async def _lock_purchasable_products(
        self, lines: list[_RequestedLine]
    ) -> dict[uuid.UUID, Product]:
        """Lock the product rows and check they are actually purchasable.

        The lock is taken before the availability check so the check and the
        later decrement cannot be interleaved with another order's.
        """
        products = await self.orders.lock_products([line.product_id for line in lines])
        for line in lines:
            if line.product_id not in products:
                raise NotFoundError("Product not found", error_code="product_not_found")
            if not products[line.product_id].is_active:
                # Same 404 as a missing product: a delisted product must not be
                # discoverable through checkout.
                raise NotFoundError("Product not found", error_code="product_not_found")
        return products

    def _price_lines(
        self, lines: list[_RequestedLine], products: dict[uuid.UUID, Product]
    ) -> list[OrderItem]:
        """Price each line from the catalogue, refusing to oversell.

        This reads ``stock_quantity`` from rows that are already locked, so the
        value it compares against cannot change before the commit.
        """
        priced: list[OrderItem] = []
        for line in lines:
            product = products[line.product_id]
            if line.quantity > product.stock_quantity:
                raise ConflictError(
                    f"Only {product.stock_quantity} of {product.name} left in stock",
                    error_code="insufficient_stock",
                    details={
                        "product_id": str(product.id),
                        "product_name": product.name,
                        "requested": line.quantity,
                        "available": product.stock_quantity,
                    },
                )
            priced.append(
                OrderItem(
                    product_id=product.id,
                    product_name=product.name,
                    unit_price=product.price,
                    quantity=line.quantity,
                    line_total=product.price * line.quantity,
                )
            )
        return priced

    async def _release_stock(self, order: Order) -> None:
        """Return the ordered quantities to the catalogue, exactly once."""
        products = await self.orders.lock_products([item.product_id for item in order.items])
        for item in order.items:
            product = products.get(item.product_id)
            if product is not None:
                product.stock_quantity += item.quantity
        await self.session.flush()

    async def _consume_cart_lines(
        self, user_id: uuid.UUID, product_ids: list[uuid.UUID]
    ) -> None:
        """Remove the purchased lines from the server cart.

        Placing an order consumes the items it bought. Removing exactly the
        ordered products (rather than emptying the cart) means a shopper who
        ordered one of two things keeps the other.
        """
        for product_id in product_ids:
            await self.carts.delete_by_product_id(user_id, product_id)


__all__ = ["OrderService", "OrderTotals", "compute_totals"]

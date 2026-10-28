"""Order models: the order header and its line items.

An order is a **financial record**, and that single sentence drives most of the
design decisions in this module:

* **Line prices are snapshotted.** ``OrderItem`` stores ``product_name`` and
  ``unit_price`` alongside the ``product_id``. A cart prices live from the
  catalogue, so it always shows today's price; an order must instead show the
  price that was actually charged, even after the catalogue is edited. The
  ``product_id`` is kept for analytics and for rendering, never for pricing.
* **Totals are checked by the database.** ``total = subtotal + delivery_charge
  - discount`` and ``line_total = unit_price * quantity`` are ``CHECK``
  constraints, so a bug in the service cannot persist an order whose parts do
  not add up to its total.
* **``order_items.product_id`` is ``RESTRICT`` while ``cart_items.product_id``
  is ``CASCADE``.** A cart line is disposable — the customer can rebuild it. An
  order line is money that has been charged, and it must not disappear because
  a product row was removed. (Phase 3 delists products rather than deleting
  them, so in practice this fires only on a hard delete.)
* **The delivery address is copied onto the order.** An address is the
  customer's own editable record; if they edit or delete it afterwards, the
  order must still show where it was going to be delivered.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base
from .payment import PaymentStatus, payment_status_enum


class OrderStatus(StrEnum):
    """Lifecycle of an order.

    The frontend types this as a five-value union and keys a badge class off
    every member with a total ``Record``, so an unexpected value renders
    ``undefined`` as a CSS class. It is therefore a native PostgreSQL enum --
    the database refuses anything outside this set, not just the API layer.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class PaymentMethod(StrEnum):
    """How the customer intends to pay.

    ``ONLINE`` orders are created ``pending`` and wait for the Razorpay phase to
    confirm payment; ``COD`` orders are actionable immediately.
    """

    COD = "cod"
    ONLINE = "online"


order_status_enum = SAEnum(
    OrderStatus,
    name="order_status",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    native_enum=True,
)

payment_method_enum = SAEnum(
    PaymentMethod,
    name="payment_method",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    native_enum=True,
)

#: The only legal status moves. Everything else is a 409.
#:
#: The order lifecycle runs forwards through the fulfilment states, and
#: cancellation is available from every state that has not yet been handed to
#: the courier. ``DELIVERED`` and ``CANCELLED`` are terminal.
ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING: frozenset(
        {OrderStatus.CONFIRMED, OrderStatus.CANCELLED}
    ),
    OrderStatus.CONFIRMED: frozenset(
        {OrderStatus.SHIPPED, OrderStatus.CANCELLED}
    ),
    OrderStatus.SHIPPED: frozenset(
        {OrderStatus.DELIVERED, OrderStatus.CANCELLED}
    ),
    OrderStatus.DELIVERED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
}

#: Statuses whose stock is still committed to this order, and which therefore
#: have to be returned to the catalogue if the order is cancelled.
STOCK_HELD_STATUSES = frozenset(
    {
        OrderStatus.PENDING,
        OrderStatus.CONFIRMED,
        OrderStatus.SHIPPED,
        OrderStatus.DELIVERED,
    }
)

#: Statuses a customer may still cancel from. Mirrors the transition table but
#: is kept separate because the *customer* has a narrower right to cancel than
#: an administrator does.
CANCELLABLE_STATUSES = frozenset(
    {OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.SHIPPED}
)


class Order(Base):
    # Fetch server-generated values (created_at, updated_at, and the
    # `onupdate` timestamp) with RETURNING during flush. Without this they
    # stay expired after a flush, and reading one in an async context raises
    # MissingGreenlet - a 503 in production.
    __mapper_args__ = {"eager_defaults": True}
    """One placed order: who bought what, for how much, delivered where."""

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        CheckConstraint("delivery_charge >= 0", name="delivery_charge_non_negative"),
        CheckConstraint("discount >= 0", name="discount_non_negative"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        # The invariant the rest of the schema exists to protect: the stored
        # total must be exactly the sum of its parts.
        CheckConstraint(
            "total = subtotal + delivery_charge - discount", name="total_consistent"
        ),
        CheckConstraint("char_length(customer_name) > 0", name="customer_name_not_empty"),
        CheckConstraint("char_length(address_line1) > 0", name="address_line1_not_empty"),
        CheckConstraint("char_length(pincode) = 6", name="pincode_is_six_digits"),
        # A user's own order history, newest first.
        Index("ix_orders_user_created_at", "user_id", "created_at"),
        # Phase 8 filters the admin order list by status.
        Index("ix_orders_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[OrderStatus] = mapped_column(
        order_status_enum,
        nullable=False,
        default=OrderStatus.PENDING,
        server_default=OrderStatus.PENDING.value,
    )
    payment_method: Mapped[PaymentMethod] = mapped_column(
        payment_method_enum, nullable=False
    )
    #: Denormalised summary of the latest payment row, so the order list does
    #: not need a join. ``NOT_REQUIRED`` for COD, ``NOT_STARTED`` until the
    #: provider order is created.
    payment_status: Mapped[PaymentStatus] = mapped_column(
        payment_status_enum,
        nullable=False,
        default=PaymentStatus.NOT_STARTED,
        server_default=PaymentStatus.NOT_STARTED.value,
    )

    # --- customer snapshot: the account may be edited or renamed later, but the
    # order must still show who it was placed by and how they were contacted.
    customer_name: Mapped[str] = mapped_column(String(150), nullable=False)
    customer_email: Mapped[str] = mapped_column(String(320), nullable=False)
    customer_phone: Mapped[str] = mapped_column(String(20), nullable=False)

    # --- address snapshot, flattened out of the nested request body.
    address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(100), nullable=False)
    pincode: Mapped[str] = mapped_column(String(10), nullable=False)
    country: Mapped[str] = mapped_column(
        String(2), nullable=False, default="IN", server_default="IN"
    )

    # --- money, all NUMERIC(12,2) and all computed server-side.
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    delivery_charge: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default="0.00"
    )
    #: Always 0.00 until a promotion phase exists. The column exists now so the
    #: response shape the frontend already renders does not change later.
    discount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0.00"), server_default="0.00"
    )
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    items: Mapped[list["OrderItem"]] = relationship(  # noqa: F821
        back_populates="order",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="OrderItem.id",
    )
    status_history: Mapped[list["OrderStatusHistory"]] = relationship(  # noqa: F821
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )

    # ------------------------------------------------------------------ views
    @property
    def item_count(self) -> int:
        """Total units across every line."""
        return sum(item.quantity for item in self.items)

    @property
    def line_count(self) -> int:
        return len(self.items)

    @property
    def address_display(self) -> str:
        """The one-line address string the frontend renders.

        The server owns this formatting. The Angular client used to build the
        same string itself at ``order.service.ts``, which is exactly how the two
        copies drift; there is now only one implementation.
        """
        parts = [self.address_line1]
        if self.address_line2:
            parts.append(self.address_line2)
        parts.extend([self.city, self.state, self.pincode])
        if self.country and self.country.upper() != "IN":
            parts.append(self.country.upper())
        return ", ".join(part for part in parts if part)

    @property
    def is_cancellable(self) -> bool:
        """Whether the *customer* may still cancel this order.

        A paid order is excluded: cancelling it would leave the customer's money
        with us. It has to go through the refund path, which releases the stock
        after the provider has accepted the refund.
        """
        if self.payment_status is PaymentStatus.PAID:
            return False
        return self.status in CANCELLABLE_STATUSES

    def can_transition_to(self, target: OrderStatus) -> bool:
        return target in ALLOWED_TRANSITIONS.get(self.status, frozenset())

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} user_id={self.user_id} "
            f"status={self.status} total={self.total}>"
        )


class OrderItem(Base):
    """One product at one quantity on one order, priced at placement time."""

    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price > 0", name="unit_price_positive"),
        CheckConstraint("line_total >= 0", name="line_total_non_negative"),
        # The line total is derived, not trusted: it must equal price x quantity.
        CheckConstraint(
            "line_total = unit_price * quantity", name="line_total_consistent"
        ),
        CheckConstraint("char_length(product_name) > 0", name="product_name_not_empty"),
        Index("ix_order_items_order_id", "order_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        # RESTRICT, not CASCADE: see the module docstring. Charged money must
        # not be deleted along with a catalogue row.
        ForeignKey("products.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: Snapshots. These, not the live product row, are what the customer was
    #: charged and what the order history will keep showing.
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    order: Mapped["Order"] = relationship(back_populates="items")
    #: Loaded for rendering (image, category). Never used for pricing.
    product: Mapped["Product"] = relationship(lazy="selectin")  # noqa: F821

    def __repr__(self) -> str:
        return f"<OrderItem order_id={self.order_id} product_id={self.product_id} x{self.quantity}>"


class OrderStatusHistory(Base):
    """An append-only audit trail of status changes.

    Added with the orders themselves rather than deferred: a status machine
    without a record of how it got there is a status machine you cannot debug
    when a customer disputes an order.
    """

    __tablename__ = "order_status_history"
    __table_args__ = (Index("ix_order_status_history_order_id", "order_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: ``None`` for the synthetic entry recorded when the order is created.
    from_status: Mapped[OrderStatus | None] = mapped_column(
        order_status_enum, nullable=True
    )
    to_status: Mapped[OrderStatus] = mapped_column(order_status_enum, nullable=False)

    #: ``user_id`` of whoever caused the change, or ``None`` for the system
    #: (order placement, and any Phase 6 payment webhook).
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: ``clock_timestamp()`` rather than ``now()``: PostgreSQL's ``now()`` is the
    #: *transaction* start time, so every row written in one transaction shares a
    #: timestamp and this audit trail cannot be ordered. It is read in sequence
    #: order, so each row needs a distinct, increasing value.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )

    order: Mapped["Order"] = relationship(back_populates="status_history")

    def __repr__(self) -> str:
        return f"<OrderStatusHistory order_id={self.order_id} -> {self.to_status}>"


__all__ = [
    "ALLOWED_TRANSITIONS",
    "CANCELLABLE_STATUSES",
    "STOCK_HELD_STATUSES",
    "Order",
    "OrderItem",
    "OrderStatus",
    "OrderStatusHistory",
    "PaymentMethod",
    "order_status_enum",
    "payment_method_enum",
]

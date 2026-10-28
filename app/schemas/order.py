"""Order request/response schemas.

The request carries no prices. It carries *what* to buy (``product_id`` and
``quantity``) and where to send it; the server decides what it costs. This is
the same rule the cart follows, and it exists because the Angular client used to
compute every total itself.

``items`` is optional. When it is omitted the order is built from the server's
cart, which is the authoritative path. When the client sends it -- which the
current frontend does, because it treats ``localStorage`` as the source of truth
-- the same validation runs against the same rows, and the purchased lines are
still removed from the server cart afterwards.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from ..models.order import OrderStatus, PaymentMethod
from ..models.payment import PaymentStatus
from .cart import MAX_CART_QUANTITY
from .product import ProductOut

#: Indian mobile number. Mirrored from the frontend's ``checkout.component.ts``
#: validation, which the server has to enforce independently.
PHONE_PATTERN = r"^[6-9]\d{9}$"
PINCODE_PATTERN = r"^\d{6}$"


class ShippingAddressIn(BaseModel):
    """The delivery address, nested in the request as the frontend sends it."""

    model_config = ConfigDict(str_strip_whitespace=True)

    line1: str = Field(min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    pincode: str = Field(pattern=PINCODE_PATTERN)
    country: str = Field(default="IN", min_length=2, max_length=2)

    @field_validator("country")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @field_validator("line2")
    @classmethod
    def _blank_line2_is_absent(cls, value: str | None) -> str | None:
        """Store "no second line" as NULL rather than as an empty string."""
        return value or None


class OrderItemIn(BaseModel):
    """One requested line. No price: the catalogue is the only source of one."""

    model_config = ConfigDict(str_strip_whitespace=True)

    product_id: uuid.UUID
    quantity: int = Field(ge=1, le=MAX_CART_QUANTITY)


class OrderCreate(BaseModel):
    """Place an order.

    ``items`` omitted means "use my server cart". There is no ``notes`` field
    because the frontend does not send one and inventing one here would only
    create a field nobody reads.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=150)
    email: EmailStr
    phone: str = Field(pattern=PHONE_PATTERN)
    address: ShippingAddressIn
    payment_method: PaymentMethod
    items: list[OrderItemIn] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Name must be at least 2 characters")
        return value

    @model_validator(mode="after")
    def _distinct_products(self) -> "OrderCreate":
        """Reject a repeated product rather than silently merging the lines.

        Merging would be friendlier, but it hides a bug in a client that is
        already sending ``quantity`` as a delta somewhere else. Failing loudly
        is cheaper than an order nobody can explain.
        """
        if self.items is None:
            return self
        seen: set[uuid.UUID] = set()
        for line in self.items:
            if line.product_id in seen:
                raise ValueError(
                    "Each product may appear at most once; combine the quantities"
                )
            seen.add(line.product_id)
        return self


class OrderItemOut(BaseModel):
    """One order line.

    The snapshot fields (``product_name``, ``unit_price``, ``line_total``) are
    what the customer was charged and never change. ``product`` is the live
    catalogue row, included so a client can render an image without a second
    request; it is nullable because the product may since have been removed.
    """

    model_config = ConfigDict(from_attributes=True)

    product_id: uuid.UUID
    product_name: str
    unit_price: Decimal
    quantity: int
    line_total: Decimal
    product: ProductOut | None = None

    @classmethod
    def from_entity(cls, item) -> "OrderItemOut":
        product = getattr(item, "product", None)
        return cls(
            product_id=item.product_id,
            product_name=item.product_name,
            unit_price=item.unit_price,
            quantity=item.quantity,
            line_total=item.line_total,
            product=ProductOut.from_entity(product) if product is not None else None,
        )


class ShippingAddressOut(BaseModel):
    """The structured delivery address as stored on the order."""

    model_config = ConfigDict(from_attributes=True)

    line1: str
    line2: str | None = None
    city: str
    state: str
    pincode: str
    country: str

    @classmethod
    def from_entity(cls, order) -> "ShippingAddressOut":
        return cls(
            line1=order.address_line1,
            line2=order.address_line2,
            city=order.city,
            state=order.state,
            pincode=order.pincode,
            country=order.country,
        )


class OrderOut(BaseModel):
    """A placed order.

    ``address`` is the pre-formatted single-line string the frontend renders,
    and it is produced by the server (see ``Order.address_display``). The
    structured form is also available as ``shipping_address``. The Angular
    client used to build this string itself; there is now one implementation.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    status: OrderStatus
    payment_method: PaymentMethod
    payment_status: PaymentStatus

    customer_name: str
    customer_email: str
    customer_phone: str

    #: Flat display string, matching the frontend's `address` field.
    address: str
    shipping_address: ShippingAddressOut

    items: list[OrderItemOut] = Field(default_factory=list)
    line_count: int = 0
    item_count: int = 0

    subtotal: Decimal
    delivery_charge: Decimal
    discount: Decimal
    total: Decimal

    cancelled_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_cancellable(self) -> bool:
        """Whether the customer may cancel. Server-owned, so the UI cannot
        reimplement the state machine and get it wrong."""
        if self.payment_status is PaymentStatus.PAID:
            return False
        return self.status in {
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
            OrderStatus.SHIPPED,
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def free_delivery_applied(self) -> bool:
        return self.delivery_charge == Decimal("0.00") and self.subtotal > 0

    @classmethod
    def from_entity(cls, order) -> "OrderOut":
        items = [OrderItemOut.from_entity(item) for item in order.items]
        return cls(
            id=order.id,
            user_id=order.user_id,
            status=order.status,
            payment_method=order.payment_method,
            payment_status=order.payment_status,
            customer_name=order.customer_name,
            customer_email=order.customer_email,
            customer_phone=order.customer_phone,
            address=order.address_display,
            shipping_address=ShippingAddressOut.from_entity(order),
            items=items,
            line_count=len(items),
            item_count=sum(item.quantity for item in items),
            subtotal=order.subtotal,
            delivery_charge=order.delivery_charge,
            discount=order.discount,
            total=order.total,
            cancelled_at=order.cancelled_at,
            delivered_at=order.delivered_at,
            created_at=order.created_at,
            updated_at=order.updated_at,
        )


class OrderPage(BaseModel):
    """Envelope ``data`` for a paginated order listing."""

    orders: list[OrderOut] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    limit: int = 20
    pages: int = 0

    @classmethod
    def build(
        cls, orders: list[OrderOut], *, total: int, page: int, limit: int
    ) -> "OrderPage":
        pages = (total + limit - 1) // limit if limit else 0
        return cls(orders=orders, total=total, page=page, limit=limit, pages=pages)


__all__ = [
    "PHONE_PATTERN",
    "PINCODE_PATTERN",
    "OrderCreate",
    "OrderItemIn",
    "OrderItemOut",
    "OrderOut",
    "OrderPage",
    "ShippingAddressIn",
    "ShippingAddressOut",
]

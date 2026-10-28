"""Payment model.

A payment is not an order status. ``orders.payment_status`` is the cheap,
list-friendly summary; the ``payments`` row is the provider-facing record that
proves what Razorpay was asked to charge and what it told us happened.

Two invariants matter more than the rest:

* **The amount is snapshotted.** It is copied from ``orders.total`` when the
  Razorpay order is created. Editing an order afterwards (or changing the
  catalogue) must not silently change what the customer was asked to pay.
* **Provider identifiers are unique.** ``provider_order_id`` and (once known)
  ``provider_payment_id`` are unique in the database, so a replayed webhook or
  a double-submitted verification cannot create a second financial record.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base


class PaymentStatus(StrEnum):
    """State of a payment, or of an order's payment obligation.

    ``NOT_REQUIRED`` and ``NOT_STARTED`` only ever appear on an order: a COD
    order does not need a payment, and an online order that has not reached the
    provider yet has no payment record.
    """

    NOT_REQUIRED = "not_required"
    NOT_STARTED = "not_started"
    CREATED = "created"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


payment_status_enum = SAEnum(
    PaymentStatus,
    name="payment_status",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    native_enum=True,
)


class Payment(Base):
    """One Razorpay payment attempt for one order.

    One row per order (``order_id`` is unique). A failed attempt can be retried
    against the same Razorpay order, which is why the row is updated rather than
    replaced -- the provider order id is stable for the retry window.
    """

    __mapper_args__ = {"eager_defaults": True}

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint(
            "order_id", name="uq_payments_order_id"
        ),
        UniqueConstraint(
            "provider_order_id", name="uq_payments_provider_order_id"
        ),
        UniqueConstraint(
            "provider_payment_id", name="uq_payments_provider_payment_id"
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("char_length(currency) = 3", name="currency_is_three_letters"),
        Index("ix_payments_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="razorpay", server_default="razorpay"
    )
    provider_order_id: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    provider_refund_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )

    #: Snapshot in rupees; Razorpay itself is called with paise.
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, default="INR", server_default="INR"
    )

    status: Mapped[PaymentStatus] = mapped_column(
        payment_status_enum,
        nullable=False,
        default=PaymentStatus.CREATED,
        server_default=PaymentStatus.CREATED.value,
    )
    signature_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    #: Provider-reported method (card, upi, netbanking, ...). Kept for support.
    method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refunded_at: Mapped[datetime | None] = mapped_column(
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

    def __repr__(self) -> str:
        return (
            f"<Payment id={self.id} order_id={self.order_id} "
            f"status={self.status} razorpay_order={self.provider_order_id}>"
        )


__all__ = ["Payment", "PaymentStatus", "payment_status_enum"]

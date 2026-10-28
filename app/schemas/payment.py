"""Payment request/response schemas.

The create response carries exactly what Razorpay's checkout script needs
(``key_id`` and ``razorpay_order_id``) plus the amount for display. The secret is
never part of any response, log line, or error detail.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from ..models.payment import PaymentStatus
from .order import OrderOut


class RazorpayOrderOut(BaseModel):
    """Everything the browser needs to open Razorpay checkout."""

    payment_id: uuid.UUID
    order_id: uuid.UUID
    razorpay_order_id: str
    key_id: str
    amount: Decimal
    amount_paise: int
    currency: str
    status: PaymentStatus

    @classmethod
    def build(cls, payment, *, key_id: str) -> "RazorpayOrderOut":
        return cls(
            payment_id=payment.id,
            order_id=payment.order_id,
            razorpay_order_id=payment.provider_order_id,
            key_id=key_id,
            amount=payment.amount,
            amount_paise=to_paise(payment.amount),
            currency=payment.currency,
            status=payment.status,
        )


class PaymentVerifyIn(BaseModel):
    """The three values Razorpay's checkout handler receives on success."""

    model_config = ConfigDict(str_strip_whitespace=True)

    razorpay_order_id: str = Field(min_length=1, max_length=100)
    razorpay_payment_id: str = Field(min_length=1, max_length=100)
    razorpay_signature: str = Field(min_length=1, max_length=200)


class PaymentOut(BaseModel):
    """A provider payment record, safe to show its owner."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    provider: str
    provider_order_id: str
    provider_payment_id: str | None = None
    provider_refund_id: str | None = None
    amount: Decimal
    currency: str
    status: PaymentStatus
    signature_verified: bool
    method: str | None = None
    failure_code: str | None = None
    failure_reason: str | None = None
    paid_at: datetime | None = None
    failed_at: datetime | None = None
    refunded_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class PaymentResult(BaseModel):
    """A verified/refunded payment together with the order it changed."""

    order: OrderOut
    payment: PaymentOut


class WebhookAck(BaseModel):
    """Webhook responses are acknowledgement, never a receipt of trust."""

    event: str
    handled: bool
    status: PaymentStatus | None = None


def to_paise(amount: Decimal) -> int:
    """Convert rupees to the integer paise Razorpay expects, without floats."""
    return int((amount * 100).to_integral_value())


__all__ = [
    "PaymentOut",
    "PaymentResult",
    "PaymentVerifyIn",
    "RazorpayOrderOut",
    "WebhookAck",
    "to_paise",
]

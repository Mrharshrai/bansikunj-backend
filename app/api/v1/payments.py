"""Payment endpoints.

Every order-scoped route is owner-scoped: another customer's order id is a 404,
not a 403. The webhook is the one public endpoint; it is authenticated by the
provider signature over the raw body, not by a bearer token.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request, status

from ...core.config import Settings
from ...core.dependencies import CurrentUser, DbSessionDep, SettingsDep
from ...core.envelope import SuccessEnvelope
from ...core.exceptions import ServiceUnavailableError
from ...schemas.payment import (
    PaymentOut,
    PaymentResult,
    PaymentVerifyIn,
    RazorpayOrderOut,
    WebhookAck,
)
from ...schemas.order import OrderOut
from ...services.payment_service import PaymentService
from ...services.razorpay_client import RazorpayClient

router = APIRouter(prefix="/payments", tags=["payments"])

PaymentOrderIdPath = Annotated[uuid.UUID, Path(description="Order id")]


def get_payment_gateway(settings: SettingsDep) -> RazorpayClient:
    """The provider client. Tests override this with a fake gateway."""
    return RazorpayClient(settings)


GatewayDep = Annotated[RazorpayClient, Depends(get_payment_gateway)]


def get_payment_service(
    session: DbSessionDep, settings: SettingsDep, gateway: GatewayDep
) -> PaymentService:
    return PaymentService(session, settings=settings, gateway=gateway)


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]


@router.post(
    "/orders/{order_id}/razorpay",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[RazorpayOrderOut],
    summary="Create a Razorpay order",
)
async def create_razorpay_order(
    order_id: PaymentOrderIdPath,
    service: PaymentServiceDep,
    user: CurrentUser,
    settings: SettingsDep,
) -> SuccessEnvelope[RazorpayOrderOut]:
    """Create (or return) the provider order for an unpaid online order.

    The amount is taken from the stored order total, not from the request. A COD
    order is a 409: there is nothing to pay online.
    """
    if settings.RAZORPAY_KEY_ID is None:  # pragma: no cover - defensive only
        raise ServiceUnavailableError(
            "Online payments are not configured",
            error_code="payments_not_configured",
        )
    payment = await service.create_for_order(user.id, order_id)
    return SuccessEnvelope[RazorpayOrderOut](
        message="Razorpay order created",
        data=RazorpayOrderOut.build(
            payment, key_id=settings.RAZORPAY_KEY_ID.get_secret_value()
        ),
    )


@router.post(
    "/orders/{order_id}/verify",
    response_model=SuccessEnvelope[PaymentResult],
    summary="Verify a Razorpay checkout callback",
)
async def verify_payment(
    order_id: PaymentOrderIdPath,
    payload: PaymentVerifyIn,
    service: PaymentServiceDep,
    user: CurrentUser,
) -> SuccessEnvelope[PaymentResult]:
    """Verify the checkout signature and confirm the order.

    Verification is idempotent: repeating the same successful callback returns
    the already-confirmed order rather than advancing it twice.
    """
    order, payment = await service.verify(user.id, order_id, payload)
    return SuccessEnvelope[PaymentResult](
        message="Payment verified successfully",
        data=PaymentResult(
            order=OrderOut.from_entity(order),
            payment=PaymentOut.model_validate(payment),
        ),
    )


@router.get(
    "/orders/{order_id}",
    response_model=SuccessEnvelope[PaymentOut],
    summary="Get an order's payment",
)
async def get_payment(
    order_id: PaymentOrderIdPath,
    service: PaymentServiceDep,
    user: CurrentUser,
) -> SuccessEnvelope[PaymentOut]:
    payment = await service.get_for_user(user.id, order_id)
    return SuccessEnvelope[PaymentOut](
        message="Payment retrieved successfully",
        data=PaymentOut.model_validate(payment),
    )


@router.post(
    "/orders/{order_id}/refund",
    response_model=SuccessEnvelope[PaymentResult],
    summary="Refund a paid order and cancel it",
)
async def refund_payment(
    order_id: PaymentOrderIdPath,
    service: PaymentServiceDep,
    user: CurrentUser,
) -> SuccessEnvelope[PaymentResult]:
    """Refund the captured payment, then cancel and restock the order.

    The provider is called before the order changes. A rejected refund leaves
    both the order and the stock untouched.
    """
    order, payment = await service.refund(user.id, order_id)
    return SuccessEnvelope[PaymentResult](
        message="Payment refunded and order cancelled",
        data=PaymentResult(
            order=OrderOut.from_entity(order),
            payment=PaymentOut.model_validate(payment),
        ),
    )


@router.post(
    "/webhook/razorpay",
    response_model=SuccessEnvelope[WebhookAck],
    summary="Razorpay webhook",
)
async def razorpay_webhook(
    request: Request,
    service: PaymentServiceDep,
    signature: Annotated[
        str | None, Header(alias="X-Razorpay-Signature")
    ] = None,
) -> SuccessEnvelope[WebhookAck]:
    """Apply a provider event after verifying its signature over the raw body."""
    raw_body = await request.body()
    ack = await service.handle_webhook(raw_body, signature)
    return SuccessEnvelope[WebhookAck](
        message="Webhook received", data=ack
    )


__all__ = ["router", "get_payment_gateway"]

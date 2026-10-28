"""Razorpay payment flows.

The security-critical operations live here so there is one implementation of
each rule:

* **Signature verification** is HMAC-SHA256 over ``"<razorpay_order_id>|
  <razorpay_payment_id>"`` with the API key secret, compared in constant time.
  A checkout callback is just a browser claim until this passes.
* **Webhook verification** is HMAC-SHA256 over the *raw request body* with the
  webhook secret. Parsing and re-serialising before hashing would break the
  signature, so the endpoint hands this service the bytes it received.
* **Idempotency** comes from the payment row's unique provider identifiers and
  a row lock. Razorpay retries webhooks; a second ``payment.captured`` must not
  write a second payment, and must not confirm the order twice.

Payment capture confirms a pending order. A paid order is no longer cancellable
by the customer through the ordinary path; :meth:`refund` refunds first, then
cancels and releases stock, so the money and the inventory move together.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import Settings, get_settings
from ..core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)
from ..core.logging import get_logger
from ..models.order import CANCELLABLE_STATUSES, Order, OrderStatus, PaymentMethod
from ..models.payment import Payment, PaymentStatus
from ..repositories.order_repository import OrderRepository
from ..repositories.payment_repository import PaymentRepository
from ..schemas.payment import PaymentVerifyIn, WebhookAck, to_paise
from .order_service import OrderService
from .razorpay_client import PaymentGateway, RazorpayClient

logger = get_logger(__name__)

PAID_EVENTS = frozenset({"payment.captured", "order.paid"})
FAILED_EVENTS = frozenset({"payment.failed"})


def compute_checkout_signature(
    razorpay_order_id: str, razorpay_payment_id: str, key_secret: str
) -> str:
    """The signature Razorpay checkout returns alongside a successful payment."""
    message = f"{razorpay_order_id}|{razorpay_payment_id}".encode()
    return hmac.new(key_secret.encode(), message, hashlib.sha256).hexdigest()


def verify_checkout_signature(
    razorpay_order_id: str,
    razorpay_payment_id: str,
    signature: str,
    key_secret: str,
) -> bool:
    expected = compute_checkout_signature(
        razorpay_order_id, razorpay_payment_id, key_secret
    )
    return hmac.compare_digest(expected, signature)


def compute_webhook_signature(raw_body: bytes, webhook_secret: str) -> str:
    return hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    raw_body: bytes, signature: str, webhook_secret: str
) -> bool:
    expected = compute_webhook_signature(raw_body, webhook_secret)
    return hmac.compare_digest(expected, signature)


class PaymentService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
        gateway: PaymentGateway | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.gateway = gateway or RazorpayClient(self.settings)
        self.payments = PaymentRepository(session)
        self.order_repository = OrderRepository(session)
        self.order_service = OrderService(session, self.settings)

    # ------------------------------------------------------------------ read
    async def get_for_user(self, user_id: uuid.UUID, order_id: uuid.UUID) -> Payment:
        """The payment for one of the caller's own orders, if one exists."""
        order = await self.order_service.get(user_id, order_id)
        payment = await self.payments.get_by_order_id(order.id)
        if payment is None:
            raise NotFoundError(
                "No payment exists for this order", error_code="payment_not_found"
            )
        return payment

    # ---------------------------------------------------------------- create
    async def create_for_order(self, user_id: uuid.UUID, order_id: uuid.UUID) -> Payment:
        """Create (or reuse) a Razorpay order for an online order.

        The order row is locked first, so two simultaneous "pay" clicks cannot
        both create a provider order and race the unique constraint.
        """
        order = await self.order_service.get(user_id, order_id)
        await self._lock_order(order.id)

        if order.payment_method is not PaymentMethod.ONLINE:
            raise ConflictError(
                "This order does not require an online payment",
                error_code="payment_not_required",
            )
        if order.status is not OrderStatus.PENDING:
            raise ConflictError(
                f"An order that is {order.status.value} cannot be paid for",
                error_code="order_not_payable",
                details={"status": order.status.value},
            )
        if order.payment_status is PaymentStatus.PAID:
            raise ConflictError(
                "This order has already been paid", error_code="order_already_paid"
            )
        if order.payment_status is PaymentStatus.REFUNDED:
            raise ConflictError(
                "This order has been refunded", error_code="order_already_refunded"
            )

        existing = await self.payments.get_by_order_id(order.id)
        if existing is not None:
            if existing.provider_order_id and existing.status in {
                PaymentStatus.CREATED,
                PaymentStatus.FAILED,
            }:
                # Retry against the same Razorpay order. The amount is fixed at
                # creation, so a changed total must not silently reuse it.
                if existing.amount != order.total:
                    raise ConflictError(
                        "The order total changed after the payment was created",
                        error_code="payment_amount_mismatch",
                    )
                order.payment_status = PaymentStatus.CREATED
                self._set_failed_attempt_open(existing)
                await self.session.flush()
                return existing
            raise ConflictError(
                "A payment for this order already exists",
                error_code="payment_already_exists",
                details={"status": existing.status.value},
            )

        result = await self.gateway.create_order(
            amount_paise=to_paise(order.total),
            receipt=str(order.id),
            notes={"order_id": str(order.id)},
        )

        payment = Payment(
            order_id=order.id,
            provider="razorpay",
            provider_order_id=str(result["id"]),
            amount=order.total,
            currency=str(result.get("currency") or "INR"),
            status=PaymentStatus.CREATED,
        )
        await self.payments.create(payment)
        order.payment_status = PaymentStatus.CREATED
        await self.session.flush()

        logger.info(
            "payment.order_created",
            order_id=str(order.id),
            payment_id=str(payment.id),
            razorpay_order_id=payment.provider_order_id,
            amount=str(payment.amount),
        )
        return payment

    # ---------------------------------------------------------------- verify
    async def verify(
        self, user_id: uuid.UUID, order_id: uuid.UUID, payload: PaymentVerifyIn
    ) -> tuple[Order, Payment]:
        """Verify a checkout callback and mark the order paid.

        ``order_id`` is from the URL and ``razorpay_order_id`` from the browser:
        both must point at the same payment. A mismatch is a 404, exactly like a
        nonexistent payment, so the endpoint cannot confirm which provider order
        ids exist.
        """
        order = await self.order_service.get(user_id, order_id)
        payment = await self.payments.get_by_provider_order_id(
            payload.razorpay_order_id, for_update=True
        )
        if payment is None or payment.order_id != order.id:
            raise NotFoundError("Payment not found", error_code="payment_not_found")

        if payment.status is PaymentStatus.PAID:
            if (
                payment.provider_payment_id
                and payment.provider_payment_id != payload.razorpay_payment_id
            ):
                raise BadRequestError(
                    "This payment has already been confirmed with a different id",
                    error_code="payment_signature_invalid",
                )
            return await self._reload_order(order.id), payment

        if payment.status is PaymentStatus.REFUNDED:
            raise ConflictError(
                "This payment has already been refunded",
                error_code="payment_already_refunded",
            )

        key_secret = self._key_secret()
        if not verify_checkout_signature(
            payload.razorpay_order_id,
            payload.razorpay_payment_id,
            payload.razorpay_signature,
            key_secret,
        ):
            logger.warning(
                "payment.signature_invalid",
                order_id=str(order.id),
                razorpay_order_id=payload.razorpay_order_id,
            )
            raise BadRequestError(
                "Payment signature verification failed",
                error_code="payment_signature_invalid",
            )

        order = await self._mark_paid(
            order,
            payment,
            provider_payment_id=payload.razorpay_payment_id,
            signature_verified=True,
            note="Payment captured (Razorpay)",
        )
        logger.info(
            "payment.verified",
            order_id=str(order.id),
            payment_id=str(payment.id),
            razorpay_payment_id=payment.provider_payment_id,
        )
        return order, payment

    # --------------------------------------------------------------- webhook
    async def handle_webhook(
        self, raw_body: bytes, signature: str | None
    ) -> WebhookAck:
        """Verify and apply a Razorpay webhook.

        Unknown events and events for unknown orders are acknowledged with
        ``handled: false`` and a 200: returning an error would make Razorpay
        retry forever over something this deployment cannot act on.
        """
        if not signature:
            raise BadRequestError(
                "Missing webhook signature", error_code="payment_signature_invalid"
            )

        webhook_secret = self._webhook_secret()
        if not verify_webhook_signature(raw_body, signature, webhook_secret):
            logger.warning("payment.webhook_signature_invalid")
            raise BadRequestError(
                "Webhook signature verification failed",
                error_code="payment_signature_invalid",
            )

        try:
            payload = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequestError(
                "Webhook body is not valid JSON", error_code="invalid_webhook_payload"
            ) from exc
        if not isinstance(payload, dict):
            raise BadRequestError(
                "Webhook body is not valid JSON", error_code="invalid_webhook_payload"
            )

        event = str(payload.get("event") or "")
        payment_entity = (
            payload.get("payload", {}).get("payment", {}).get("entity", {})
            if isinstance(payload.get("payload"), dict)
            else {}
        )
        order_entity = (
            payload.get("payload", {}).get("order", {}).get("entity", {})
            if isinstance(payload.get("payload"), dict)
            else {}
        )
        if not isinstance(payment_entity, dict):
            payment_entity = {}
        if not isinstance(order_entity, dict):
            order_entity = {}

        provider_order_id = payment_entity.get("order_id") or order_entity.get("id")
        if not provider_order_id:
            logger.info(
                "payment.webhook_unhandled", webhook_event=event, reason="no_order_id"
            )
            return WebhookAck(event=event, handled=False)

        payment = await self.payments.get_by_provider_order_id(
            str(provider_order_id), for_update=True
        )
        if payment is None:
            logger.warning(
                "payment.webhook_unknown_order",
                webhook_event=event,
                razorpay_order_id=str(provider_order_id),
            )
            return WebhookAck(event=event, handled=False)

        order = await self.order_repository.get_any_by_id(payment.order_id)
        if order is None:
            # The payment cascades with its order; this is only reachable if a
            # webhook raced a delete.
            return WebhookAck(event=event, handled=False, status=payment.status)

        if event in PAID_EVENTS:
            if payment.status is PaymentStatus.REFUNDED:
                logger.warning(
                    "payment.webhook_after_refund",
                    order_id=str(order.id),
                    webhook_event=event,
                )
                return WebhookAck(event=event, handled=False, status=payment.status)
            provider_payment_id = payment_entity.get("id")
            payment.method = (
                str(payment_entity.get("method"))
                if payment_entity.get("method")
                else payment.method
            )
            order = await self._mark_paid(
                order,
                payment,
                provider_payment_id=(
                    str(provider_payment_id) if provider_payment_id else None
                ),
                signature_verified=False,
                note=f"Payment captured ({event})",
            )
            logger.info(
                "payment.webhook_captured",
                order_id=str(order.id),
                webhook_event=event,
                payment_id=str(payment.id),
            )
            return WebhookAck(event=event, handled=True, status=payment.status)

        if event in FAILED_EVENTS:
            if payment.status in {PaymentStatus.PAID, PaymentStatus.REFUNDED}:
                # Never downgrade a successful payment from a late/duplicate
                # failure event.
                return WebhookAck(event=event, handled=False, status=payment.status)
            await self._mark_failed(payment, order, payment_entity)
            logger.info(
                "payment.webhook_failed",
                order_id=str(order.id),
                webhook_event=event,
                payment_id=str(payment.id),
            )
            return WebhookAck(event=event, handled=True, status=payment.status)

        logger.info("payment.webhook_ignored", webhook_event=event)
        return WebhookAck(event=event, handled=False, status=payment.status)

    # ---------------------------------------------------------------- refund
    async def refund(
        self, user_id: uuid.UUID, order_id: uuid.UUID
    ) -> tuple[Order, Payment]:
        """Refund a captured payment, then cancel the order.

        The provider is called before the order is cancelled. If the refund is
        rejected, the order and stock are left untouched, so we never release
        stock while keeping the customer's money.
        """
        order = await self.order_service.get(user_id, order_id)
        if order.payment_status is not PaymentStatus.PAID:
            raise ConflictError(
                "This order has no captured payment to refund",
                error_code="payment_not_refundable",
            )
        if order.status not in CANCELLABLE_STATUSES:
            raise ConflictError(
                f"An order that is {order.status.value} can no longer be cancelled",
                error_code="order_not_cancellable",
                details={"status": order.status.value},
            )

        await self._lock_order(order.id)
        # The lock serialises against cancellation; re-read the status so the
        # refund decision is based on the row as it is now.
        order = await self.order_service.get(user_id, order_id)
        payment = await self.payments.get_by_order_id(order.id, for_update=True)
        if payment is None or payment.status is not PaymentStatus.PAID:
            raise ConflictError(
                "This order has no captured payment to refund",
                error_code="payment_not_refundable",
            )
        if not payment.provider_payment_id:
            raise ConflictError(
                "The provider payment id is missing; the refund cannot be issued",
                error_code="payment_not_refundable",
            )

        result = await self.gateway.refund(
            provider_payment_id=payment.provider_payment_id,
            amount_paise=to_paise(payment.amount),
            notes={"order_id": str(order.id), "reason": "customer_cancelled"},
        )

        payment.status = PaymentStatus.REFUNDED
        payment.provider_refund_id = str(result.get("id") or "") or None
        payment.refunded_at = datetime.now(timezone.utc)
        order.payment_status = PaymentStatus.REFUNDED
        await self.session.flush()

        # The refund is recorded, so the ordinary customer cancellation may now
        # run and return the stock exactly once.
        cancelled = await self.order_service.cancel(user_id, order.id)
        logger.info(
            "payment.refunded",
            order_id=str(order.id),
            payment_id=str(payment.id),
            refund_id=payment.provider_refund_id,
        )
        return cancelled, payment

    # ------------------------------------------------------------- internals
    async def _mark_paid(
        self,
        order: Order,
        payment: Payment,
        *,
        provider_payment_id: str | None,
        signature_verified: bool,
        note: str,
    ) -> Order:
        if payment.status is PaymentStatus.PAID:
            return order

        if provider_payment_id:
            if (
                payment.provider_payment_id
                and payment.provider_payment_id != provider_payment_id
            ):
                logger.warning(
                    "payment.provider_payment_id_mismatch",
                    order_id=str(order.id),
                    existing=payment.provider_payment_id,
                    incoming=provider_payment_id,
                )
                raise BadRequestError(
                    "The payment id does not match this payment",
                    error_code="payment_signature_invalid",
                )
            payment.provider_payment_id = provider_payment_id

        payment.status = PaymentStatus.PAID
        payment.signature_verified = payment.signature_verified or signature_verified
        payment.paid_at = datetime.now(timezone.utc)
        payment.failure_code = None
        payment.failure_reason = None
        order.payment_status = PaymentStatus.PAID
        await self.session.flush()

        if order.status is OrderStatus.PENDING:
            order = await self.order_service.advance(
                order,
                OrderStatus.CONFIRMED,
                changed_by=None,
                note=note,
            )
        elif order.status is OrderStatus.CANCELLED:
            # A capture after cancellation needs a refund, but confirming a
            # cancelled order would be worse. Leave the financial record intact
            # and make the anomaly loud in the logs.
            logger.error(
                "payment.captured_after_cancel",
                order_id=str(order.id),
                payment_id=str(payment.id),
            )
        return order

    async def _mark_failed(
        self, payment: Payment, order: Order, entity: dict
    ) -> None:
        payment.status = PaymentStatus.FAILED
        payment.failed_at = datetime.now(timezone.utc)
        error = entity.get("error") if isinstance(entity.get("error"), dict) else {}
        payment.failure_code = (
            str(error.get("code"))[:100]
            if error.get("code")
            else None
        )
        payment.failure_reason = (
            str(error.get("description") or error.get("reason"))[:255]
            if (error.get("description") or error.get("reason"))
            else None
        )
        order.payment_status = PaymentStatus.FAILED
        await self.session.flush()

    @staticmethod
    def _set_failed_attempt_open(payment: Payment) -> None:
        """A retry reuses the provider order, so clear the previous failure."""
        payment.status = PaymentStatus.CREATED
        payment.failed_at = None
        payment.failure_code = None
        payment.failure_reason = None

    async def _lock_order(self, order_id: uuid.UUID) -> None:
        await self.session.execute(
            select(Order.id).where(Order.id == order_id).with_for_update()
        )

    async def _reload_order(self, order_id: uuid.UUID) -> Order:
        order = await self.order_repository.get_any_by_id(order_id)
        if order is None:  # pragma: no cover - the caller already loaded it
            raise NotFoundError("Order not found", error_code="order_not_found")
        return order

    def _key_secret(self) -> str:
        secret = self.settings.RAZORPAY_KEY_SECRET
        if secret is None:
            raise ServiceUnavailableError(
                "Online payments are not configured",
                error_code="payments_not_configured",
            )
        return secret.get_secret_value()

    def _webhook_secret(self) -> str:
        secret = self.settings.RAZORPAY_WEBHOOK_SECRET
        if secret is None:
            raise ServiceUnavailableError(
                "Payment webhooks are not configured",
                error_code="payments_not_configured",
            )
        return secret.get_secret_value()


__all__ = [
    "PaymentService",
    "compute_checkout_signature",
    "compute_webhook_signature",
    "verify_checkout_signature",
    "verify_webhook_signature",
]

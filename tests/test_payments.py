"""Payment endpoint and service tests.

The provider is always a fake. What is under test is our side of the contract:
the amount comes from the order, a checkout callback is a claim until its HMAC
verifies, a webhook is idempotent, and money and stock only move together.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.payments import get_payment_gateway
from app.core.config import get_settings
from app.core.exceptions import ServiceUnavailableError
from app.main import app
from app.models.order import (
    Order,
    OrderStatus,
    OrderStatusHistory,
    PaymentMethod,
)
from app.models.payment import Payment, PaymentStatus
from app.models.user import User
from app.services.payment_service import (
    compute_checkout_signature,
    compute_webhook_signature,
)
from tests.conftest import (
    _make_user,
    auth_header_for,
    make_category,
    make_product,
)

pytestmark = pytest.mark.asyncio

PAYMENTS = "/api/v1/payments"
ORDERS = "/api/v1/orders"

KEY_ID = "rzp_test_key_id"
KEY_SECRET = "rzp_test_key_secret"
WEBHOOK_SECRET = "whsec_test_secret"

ADDRESS = {
    "line1": "12 MG Road",
    "line2": "Near Metro Station",
    "city": "Pune",
    "state": "Maharashtra",
    "pincode": "411001",
}


class FakeGateway:
    """A recording provider with deterministic, unique ids."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.refunds: list[dict] = []
        self.next_order = 0
        self.next_refund = 0
        self.fail_create = False
        self.fail_refund = False

    async def create_order(
        self, *, amount_paise: int, receipt: str, notes: dict[str, str]
    ) -> dict:
        if self.fail_create:
            raise ServiceUnavailableError(
                "The payment provider is temporarily unavailable",
                error_code="payment_provider_unavailable",
            )
        self.next_order += 1
        call = {
            "amount_paise": amount_paise,
            "receipt": receipt,
            "notes": notes,
        }
        self.created.append(call)
        return {
            "id": f"order_test_{self.next_order}",
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt,
        }

    async def refund(
        self, *, provider_payment_id: str, amount_paise: int, notes: dict[str, str]
    ) -> dict:
        if self.fail_refund:
            raise ServiceUnavailableError(
                "The payment provider is temporarily unavailable",
                error_code="payment_provider_unavailable",
            )
        self.next_refund += 1
        self.refunds.append(
            {
                "provider_payment_id": provider_payment_id,
                "amount_paise": amount_paise,
                "notes": notes,
            }
        )
        return {
            "id": f"rfnd_test_{self.next_refund}",
            "payment_id": provider_payment_id,
            "amount": amount_paise,
            "currency": "INR",
        }


def payment_settings(**overrides):
    values = {
        "RAZORPAY_KEY_ID": SecretStr(KEY_ID),
        "RAZORPAY_KEY_SECRET": SecretStr(KEY_SECRET),
        "RAZORPAY_WEBHOOK_SECRET": SecretStr(WEBHOOK_SECRET),
    }
    values.update(overrides)
    return get_settings().model_copy(update=values)


@pytest_asyncio.fixture
async def gateway() -> FakeGateway:
    return FakeGateway()


@pytest_asyncio.fixture
async def payment_client(
    client: AsyncClient, gateway: FakeGateway
) -> AsyncClient:
    """The normal test client, wired to a fake provider and known secrets."""
    app.dependency_overrides[get_settings] = lambda: payment_settings()
    app.dependency_overrides[get_payment_gateway] = lambda: gateway
    return client


def data(response) -> dict:
    return response.json()["data"]


def online_order_body(items: list[dict]) -> dict:
    return {
        "name": "Asha Menon",
        "email": "asha@example.com",
        "phone": "9876543210",
        "address": dict(ADDRESS),
        "payment_method": "online",
        "items": items,
    }


async def seed_product(session: AsyncSession, **kwargs):
    category = await make_category(session, slug="payments-cakes")
    return await make_product(session, category, **kwargs)


async def place_order(
    client: AsyncClient, headers: dict[str, str], product, *, quantity: int = 1
) -> dict:
    response = await client.post(
        ORDERS,
        json=online_order_body(
            [{"product_id": str(product.id), "quantity": quantity}]
        ),
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return data(response)


async def order_row(session: AsyncSession, order_id: uuid.UUID) -> Order:
    result = await session.execute(select(Order).where(Order.id == order_id))
    return result.scalar_one()


async def payment_row(session: AsyncSession, order_id: uuid.UUID) -> Payment | None:
    result = await session.execute(
        select(Payment).where(Payment.order_id == order_id)
    )
    return result.scalar_one_or_none()


async def history_row(session: AsyncSession, order_id: uuid.UUID) -> list[OrderStatusHistory]:
    result = await session.execute(
        select(OrderStatusHistory)
        .where(OrderStatusHistory.order_id == order_id)
        .order_by(OrderStatusHistory.created_at.asc(), OrderStatusHistory.id.asc())
    )
    return list(result.scalars().all())


async def created_payment(
    client: AsyncClient, headers: dict[str, str], product
) -> tuple[dict, dict]:
    """Place an online order and create its provider order."""
    order = await place_order(client, headers, product)
    response = await client.post(
        f"{PAYMENTS}/orders/{order['id']}/razorpay", headers=headers
    )
    assert response.status_code == 201, response.text
    return order, data(response)


async def paid_payment(
    client: AsyncClient,
    headers: dict[str, str],
    product,
    *,
    payment_id: str = "pay_test_1",
) -> tuple[dict, dict]:
    """Place, create and successfully verify an online payment."""
    order, created = await created_payment(client, headers, product)
    signature = compute_checkout_signature(
        created["razorpay_order_id"], payment_id, KEY_SECRET
    )
    response = await client.post(
        f"{PAYMENTS}/orders/{order['id']}/verify",
        json={
            "razorpay_order_id": created["razorpay_order_id"],
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return order, data(response)


def webhook_body(
    *,
    event: str,
    provider_order_id: str,
    provider_payment_id: str | None = None,
    method: str | None = "upi",
    error: dict | None = None,
) -> dict:
    payment: dict = {"order_id": provider_order_id}
    if provider_payment_id is not None:
        payment["id"] = provider_payment_id
    if method is not None:
        payment["method"] = method
    if error is not None:
        payment["error"] = error
    return {
        "event": event,
        "payload": {
            "payment": {"entity": payment},
            "order": {"entity": {"id": provider_order_id}},
        },
    }


async def post_webhook(
    client: AsyncClient, body: dict, *, secret: str = WEBHOOK_SECRET
) -> object:
    raw = json.dumps(body).encode()
    signature = compute_webhook_signature(raw, secret)
    return await client.post(
        f"{PAYMENTS}/webhook/razorpay",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
        },
    )


class TestAuth:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", f"{PAYMENTS}/orders/{uuid.uuid4()}/razorpay"),
            ("post", f"{PAYMENTS}/orders/{uuid.uuid4()}/verify"),
            ("get", f"{PAYMENTS}/orders/{uuid.uuid4()}"),
            ("post", f"{PAYMENTS}/orders/{uuid.uuid4()}/refund"),
        ],
    )
    async def test_order_payment_routes_require_auth(
        self, payment_client: AsyncClient, method: str, path: str
    ):
        response = await payment_client.request(method.upper(), path, json={})
        assert response.status_code == 401, response.text

    async def test_webhook_does_not_use_a_bearer_token_but_requires_a_signature(
        self, payment_client: AsyncClient
    ):
        response = await payment_client.post(
            f"{PAYMENTS}/webhook/razorpay",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "payment_signature_invalid"


class TestCreate:
    async def test_online_order_starts_not_started_and_cod_is_not_required(
        self, payment_client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        online = await place_order(payment_client, customer_headers, product)
        assert online["payment_status"] == "not_started"

        response = await payment_client.post(
            ORDERS,
            json={
                **online_order_body(
                    [{"product_id": str(product.id), "quantity": 1}]
                ),
                "payment_method": "cod",
            },
            headers=customer_headers,
        )
        assert response.status_code == 201, response.text
        assert data(response)["payment_status"] == "not_required"

    async def test_create_uses_the_stored_order_total_and_returns_checkout_fields(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
        gateway: FakeGateway,
    ):
        product = await seed_product(
            session, price=Decimal("199.00"), stock_quantity=10
        )
        order = await place_order(payment_client, customer_headers, product, quantity=2)
        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/razorpay",
            headers=customer_headers,
            json={"amount": 1},
        )
        assert response.status_code == 201, response.text
        created = data(response)
        assert created["key_id"] == KEY_ID
        assert created["razorpay_order_id"].startswith("order_test_")
        assert Decimal(created["amount"]) == Decimal(order["total"])
        assert created["amount_paise"] == int(Decimal(order["total"]) * 100)
        assert created["currency"] == "INR"
        assert created["status"] == "created"

        assert gateway.created == [
            {
                "amount_paise": int(Decimal(order["total"]) * 100),
                "receipt": order["id"],
                "notes": {"order_id": order["id"]},
            }
        ]
        stored = await order_row(session, uuid.UUID(order["id"]))
        assert stored.payment_status is PaymentStatus.CREATED
        assert (await payment_row(session, stored.id)).amount == stored.total

    async def test_create_is_idempotent_for_the_same_order(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
        gateway: FakeGateway,
    ):
        product = await seed_product(session, price=Decimal("199.00"))
        order = await place_order(payment_client, customer_headers, product)
        first = data(
            await payment_client.post(
                f"{PAYMENTS}/orders/{order['id']}/razorpay",
                headers=customer_headers,
            )
        )
        second = data(
            await payment_client.post(
                f"{PAYMENTS}/orders/{order['id']}/razorpay",
                headers=customer_headers,
            )
        )
        assert first["razorpay_order_id"] == second["razorpay_order_id"]
        assert len(gateway.created) == 1

    async def test_a_cod_order_cannot_be_paid_online(
        self, payment_client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        response = await payment_client.post(
            ORDERS,
            json={
                **online_order_body(
                    [{"product_id": str(product.id), "quantity": 1}]
                ),
                "payment_method": "cod",
            },
            headers=customer_headers,
        )
        order_id = data(response)["id"]

        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order_id}/razorpay", headers=customer_headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "payment_not_required"

    async def test_an_order_that_is_no_longer_pending_cannot_be_paid(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session)
        order = await place_order(payment_client, customer_headers, product)
        stored = await order_row(session, uuid.UUID(order["id"]))
        stored.status = OrderStatus.CONFIRMED
        await session.flush()

        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/razorpay", headers=customer_headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "order_not_payable"

    async def test_another_customer_cannot_create_a_payment(
        self, payment_client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session)
        owner = await _make_user(session)
        owner_headers = await auth_header_for(payment_client, owner)
        order = await place_order(payment_client, owner_headers, product)

        intruder = await _make_user(session)
        intruder_headers = await auth_header_for(payment_client, intruder)
        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/razorpay", headers=intruder_headers
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "order_not_found"

    async def test_missing_provider_configuration_is_a_503(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session)
        order = await place_order(payment_client, customer_headers, product)
        app.dependency_overrides[get_settings] = lambda: payment_settings(
            RAZORPAY_KEY_ID=None
        )

        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/razorpay", headers=customer_headers
        )
        assert response.status_code == 503
        assert response.json()["error_code"] == "payments_not_configured"

    async def test_a_provider_outage_is_a_503_and_creates_no_payment(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
        gateway: FakeGateway,
    ):
        product = await seed_product(session)
        order = await place_order(payment_client, customer_headers, product)
        gateway.fail_create = True

        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/razorpay", headers=customer_headers
        )
        assert response.status_code == 503
        assert await payment_row(session, uuid.UUID(order["id"])) is None
        stored = await order_row(session, uuid.UUID(order["id"]))
        assert stored.payment_status is PaymentStatus.NOT_STARTED


class TestVerify:
    async def test_valid_signature_confirms_the_order(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session)
        order, created = await created_payment(
            payment_client, customer_headers, product
        )
        signature = compute_checkout_signature(
            created["razorpay_order_id"], "pay_test_valid", KEY_SECRET
        )
        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/verify",
            json={
                "razorpay_order_id": created["razorpay_order_id"],
                "razorpay_payment_id": "pay_test_valid",
                "razorpay_signature": signature,
            },
            headers=customer_headers,
        )
        assert response.status_code == 200, response.text
        result = data(response)
        assert result["order"]["status"] == "confirmed"
        assert result["order"]["payment_status"] == "paid"
        assert result["order"]["is_cancellable"] is False
        assert result["payment"]["status"] == "paid"
        assert result["payment"]["provider_payment_id"] == "pay_test_valid"
        assert result["payment"]["signature_verified"] is True

        history = await history_row(session, uuid.UUID(order["id"]))
        assert [entry.to_status for entry in history] == [
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
        ]

    async def test_invalid_signature_does_not_touch_the_order(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session)
        order, created = await created_payment(
            payment_client, customer_headers, product
        )
        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/verify",
            json={
                "razorpay_order_id": created["razorpay_order_id"],
                "razorpay_payment_id": "pay_test_forged",
                "razorpay_signature": "0" * 64,
            },
            headers=customer_headers,
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "payment_signature_invalid"
        stored = await order_row(session, uuid.UUID(order["id"]))
        assert stored.status is OrderStatus.PENDING
        assert stored.payment_status is PaymentStatus.CREATED
        assert (await payment_row(session, stored.id)).status is PaymentStatus.CREATED

    async def test_repeating_the_same_success_is_idempotent(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session)
        order, paid = await paid_payment(
            payment_client, customer_headers, product, payment_id="pay_test_repeat"
        )
        created = None
        payment = await payment_row(session, uuid.UUID(order["id"]))
        created = {
            "razorpay_order_id": payment.provider_order_id,
        }
        signature = compute_checkout_signature(
            created["razorpay_order_id"], "pay_test_repeat", KEY_SECRET
        )
        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/verify",
            json={
                "razorpay_order_id": created["razorpay_order_id"],
                "razorpay_payment_id": "pay_test_repeat",
                "razorpay_signature": signature,
            },
            headers=customer_headers,
        )
        assert response.status_code == 200, response.text
        assert data(response)["order"]["status"] == "confirmed"

        history = await history_row(session, uuid.UUID(order["id"]))
        assert [entry.to_status for entry in history] == [
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
        ], "a repeated callback must not confirm the order twice"

    async def test_a_provider_order_id_from_another_order_is_a_404(
        self, payment_client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session, stock_quantity=10)
        first_user = await _make_user(session)
        first_headers = await auth_header_for(payment_client, first_user)
        first_order, first_created = await created_payment(
            payment_client, first_headers, product
        )

        second_user = await _make_user(session)
        second_headers = await auth_header_for(payment_client, second_user)
        second_order = await place_order(
            payment_client, second_headers, product
        )
        signature = compute_checkout_signature(
            first_created["razorpay_order_id"], "pay_test_cross", KEY_SECRET
        )
        response = await payment_client.post(
            f"{PAYMENTS}/orders/{second_order['id']}/verify",
            json={
                "razorpay_order_id": first_created["razorpay_order_id"],
                "razorpay_payment_id": "pay_test_cross",
                "razorpay_signature": signature,
            },
            headers=second_headers,
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "payment_not_found"
        assert (await order_row(session, uuid.UUID(first_order["id"]))).status is OrderStatus.PENDING

    async def test_another_customer_cannot_verify_my_payment(
        self, payment_client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session)
        owner = await _make_user(session)
        owner_headers = await auth_header_for(payment_client, owner)
        order, created = await created_payment(
            payment_client, owner_headers, product
        )
        signature = compute_checkout_signature(
            created["razorpay_order_id"], "pay_test_intruder", KEY_SECRET
        )

        intruder = await _make_user(session)
        intruder_headers = await auth_header_for(payment_client, intruder)
        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/verify",
            json={
                "razorpay_order_id": created["razorpay_order_id"],
                "razorpay_payment_id": "pay_test_intruder",
                "razorpay_signature": signature,
            },
            headers=intruder_headers,
        )
        assert response.status_code == 404

    async def test_the_payment_can_be_read_back(
        self, payment_client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        order, created = await created_payment(
            payment_client, customer_headers, product
        )
        response = await payment_client.get(
            f"{PAYMENTS}/orders/{order['id']}", headers=customer_headers
        )
        assert response.status_code == 200
        assert data(response)["provider_order_id"] == created["razorpay_order_id"]

        missing = await payment_client.get(
            f"{PAYMENTS}/orders/{uuid.uuid4()}", headers=customer_headers
        )
        assert missing.status_code == 404


class TestWebhook:
    async def test_capture_confirms_the_order_and_records_the_provider_id(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session)
        order, created = await created_payment(
            payment_client, customer_headers, product
        )
        response = await post_webhook(
            payment_client,
            webhook_body(
                event="payment.captured",
                provider_order_id=created["razorpay_order_id"],
                provider_payment_id="pay_webhook_1",
            ),
        )
        assert response.status_code == 200, response.text
        assert data(response)["handled"] is True
        assert data(response)["status"] == "paid"

        stored = await order_row(session, uuid.UUID(order["id"]))
        assert stored.status is OrderStatus.CONFIRMED
        assert stored.payment_status is PaymentStatus.PAID
        payment = await payment_row(session, stored.id)
        assert payment.provider_payment_id == "pay_webhook_1"
        assert payment.method == "upi"
        assert payment.signature_verified is False

    async def test_duplicate_capture_is_idempotent(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session)
        order, created = await created_payment(
            payment_client, customer_headers, product
        )
        body = webhook_body(
            event="payment.captured",
            provider_order_id=created["razorpay_order_id"],
            provider_payment_id="pay_webhook_dup",
        )
        first = await post_webhook(payment_client, body)
        second = await post_webhook(payment_client, body)
        assert first.status_code == second.status_code == 200

        history = await history_row(session, uuid.UUID(order["id"]))
        assert [entry.to_status for entry in history] == [
            OrderStatus.PENDING,
            OrderStatus.CONFIRMED,
        ]
        count = await session.scalar(
            select(func.count()).select_from(Payment)
        )
        assert count == 1

    async def test_failed_event_records_the_failure_without_cancelling(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session)
        order, created = await created_payment(
            payment_client, customer_headers, product
        )
        response = await post_webhook(
            payment_client,
            webhook_body(
                event="payment.failed",
                provider_order_id=created["razorpay_order_id"],
                error={
                    "code": "BAD_REQUEST_ERROR",
                    "description": "Card declined",
                },
            ),
        )
        assert response.status_code == 200, response.text
        assert data(response)["handled"] is True
        assert data(response)["status"] == "failed"

        stored = await order_row(session, uuid.UUID(order["id"]))
        assert stored.status is OrderStatus.PENDING
        assert stored.payment_status is PaymentStatus.FAILED
        payment = await payment_row(session, stored.id)
        assert payment.failure_code == "BAD_REQUEST_ERROR"
        assert payment.failure_reason == "Card declined"

    async def test_create_after_a_failure_reuses_the_provider_order(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
        gateway: FakeGateway,
    ):
        product = await seed_product(session)
        order, created = await created_payment(
            payment_client, customer_headers, product
        )
        await post_webhook(
            payment_client,
            webhook_body(
                event="payment.failed",
                provider_order_id=created["razorpay_order_id"],
            ),
        )
        retried = data(
            await payment_client.post(
                f"{PAYMENTS}/orders/{order['id']}/razorpay",
                headers=customer_headers,
            )
        )
        assert retried["razorpay_order_id"] == created["razorpay_order_id"]
        assert retried["status"] == "created"
        assert len(gateway.created) == 1
        stored = await order_row(session, uuid.UUID(order["id"]))
        assert stored.payment_status is PaymentStatus.CREATED

    async def test_a_late_failure_does_not_downgrade_a_paid_order(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session)
        order, paid = await paid_payment(
            payment_client, customer_headers, product, payment_id="pay_settled"
        )
        provider_order_id = paid["payment"]["provider_order_id"]

        response = await post_webhook(
            payment_client,
            webhook_body(
                event="payment.failed",
                provider_order_id=provider_order_id,
            ),
        )
        assert response.status_code == 200
        assert data(response)["handled"] is False
        assert data(response)["status"] == "paid"
        assert (await order_row(session, uuid.UUID(order["id"]))).status is OrderStatus.CONFIRMED

    async def test_unknown_event_and_unknown_order_are_acknowledged(
        self, payment_client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        order, created = await created_payment(
            payment_client, customer_headers, product
        )
        unknown_event = await post_webhook(
            payment_client,
            webhook_body(
                event="payment.authorized",
                provider_order_id=created["razorpay_order_id"],
            ),
        )
        assert unknown_event.status_code == 200
        assert data(unknown_event)["handled"] is False

        unknown_order = await post_webhook(
            payment_client,
            webhook_body(
                event="payment.captured",
                provider_order_id="order_does_not_exist",
                provider_payment_id="pay_unknown",
            ),
        )
        assert unknown_order.status_code == 200
        assert data(unknown_order)["handled"] is False

    async def test_invalid_signature_is_rejected(
        self, payment_client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        order, created = await created_payment(
            payment_client, customer_headers, product
        )
        body = webhook_body(
            event="payment.captured",
            provider_order_id=created["razorpay_order_id"],
            provider_payment_id="pay_forged",
        )
        response = await post_webhook(payment_client, body, secret="wrong_secret")
        assert response.status_code == 400
        assert response.json()["error_code"] == "payment_signature_invalid"
        stored = await order_row(session, uuid.UUID(order["id"]))
        assert stored.payment_status is PaymentStatus.CREATED

    async def test_malformed_json_with_a_valid_signature_is_a_400(
        self, payment_client: AsyncClient
    ):
        raw = b"{not json"
        signature = compute_webhook_signature(raw, WEBHOOK_SECRET)
        response = await payment_client.post(
            f"{PAYMENTS}/webhook/razorpay",
            content=raw,
            headers={"X-Razorpay-Signature": signature},
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "invalid_webhook_payload"


class TestRefundAndCancel:
    async def test_a_paid_order_cannot_be_cancelled_without_a_refund(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session, stock_quantity=10)
        order, paid = await paid_payment(
            payment_client, customer_headers, product, payment_id="pay_cancel_guard"
        )
        response = await payment_client.post(
            f"{ORDERS}/{order['id']}/cancel", headers=customer_headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "paid_order_requires_refund"
        assert (await order_row(session, uuid.UUID(order["id"]))).status is OrderStatus.CONFIRMED

    async def test_refund_calls_the_provider_then_cancels_and_restocks(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
        gateway: FakeGateway,
    ):
        product = await seed_product(
            session, price=Decimal("199.00"), stock_quantity=10
        )
        order, paid = await paid_payment(
            payment_client, customer_headers, product, payment_id="pay_refund_1"
        )
        assert product.stock_quantity == 9

        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/refund", headers=customer_headers
        )
        assert response.status_code == 200, response.text
        result = data(response)
        assert result["order"]["status"] == "cancelled"
        assert result["order"]["payment_status"] == "refunded"
        assert result["payment"]["status"] == "refunded"
        assert result["payment"]["provider_refund_id"] == "rfnd_test_1"

        assert gateway.refunds == [
            {
                "provider_payment_id": "pay_refund_1",
                "amount_paise": int(Decimal(order["total"]) * 100),
                "notes": {
                    "order_id": order["id"],
                    "reason": "customer_cancelled",
                },
            }
        ]
        await session.refresh(product)
        assert product.stock_quantity == 10

    async def test_refunding_twice_is_a_conflict_and_does_not_double_restock(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
    ):
        product = await seed_product(session, stock_quantity=10)
        order, paid = await paid_payment(
            payment_client, customer_headers, product, payment_id="pay_refund_twice"
        )
        first = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/refund", headers=customer_headers
        )
        assert first.status_code == 200
        second = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/refund", headers=customer_headers
        )
        assert second.status_code == 409
        assert second.json()["error_code"] == "payment_not_refundable"
        await session.refresh(product)
        assert product.stock_quantity == 10

    async def test_an_unpaid_order_cannot_be_refunded(
        self, payment_client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        order = await place_order(payment_client, customer_headers, product)
        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/refund", headers=customer_headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "payment_not_refundable"

    async def test_a_rejected_refund_leaves_the_order_and_stock_untouched(
        self,
        payment_client: AsyncClient,
        session: AsyncSession,
        customer_headers,
        gateway: FakeGateway,
    ):
        product = await seed_product(session, stock_quantity=10)
        order, paid = await paid_payment(
            payment_client, customer_headers, product, payment_id="pay_refund_fail"
        )
        gateway.fail_refund = True

        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/refund", headers=customer_headers
        )
        assert response.status_code == 503
        stored = await order_row(session, uuid.UUID(order["id"]))
        assert stored.status is OrderStatus.CONFIRMED
        assert stored.payment_status is PaymentStatus.PAID
        assert (await payment_row(session, stored.id)).status is PaymentStatus.PAID
        await session.refresh(product)
        assert product.stock_quantity == 9

    async def test_another_customer_cannot_refund_my_order(
        self, payment_client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session)
        owner = await _make_user(session)
        owner_headers = await auth_header_for(payment_client, owner)
        order, paid = await paid_payment(
            payment_client, owner_headers, product, payment_id="pay_refund_isolation"
        )
        intruder = await _make_user(session)
        intruder_headers = await auth_header_for(payment_client, intruder)

        response = await payment_client.post(
            f"{PAYMENTS}/orders/{order['id']}/refund", headers=intruder_headers
        )
        assert response.status_code == 404
        assert (await order_row(session, uuid.UUID(order["id"]))).status is OrderStatus.CONFIRMED


class TestDatabaseConstraints:
    async def test_provider_order_id_is_unique(
        self, session: AsyncSession, customer: User
    ):
        from sqlalchemy.exc import IntegrityError

        product = await make_product(session)
        order = Order(
            user_id=customer.id,
            payment_method=PaymentMethod.ONLINE,
            payment_status=PaymentStatus.CREATED,
            customer_name="Asha Menon",
            customer_email="asha@example.com",
            customer_phone="9876543210",
            address_line1="12 MG Road",
            city="Pune",
            state="Maharashtra",
            pincode="411001",
            subtotal=Decimal("199.00"),
            delivery_charge=Decimal("0.00"),
            discount=Decimal("0.00"),
            total=Decimal("199.00"),
        )
        session.add(order)
        await session.flush()
        first = Payment(
            order_id=order.id,
            provider_order_id="order_unique",
            amount=Decimal("199.00"),
        )
        session.add(first)
        await session.flush()

        second_order = Order(
            user_id=customer.id,
            payment_method=PaymentMethod.ONLINE,
            payment_status=PaymentStatus.CREATED,
            customer_name="Asha Menon",
            customer_email="asha@example.com",
            customer_phone="9876543210",
            address_line1="12 MG Road",
            city="Pune",
            state="Maharashtra",
            pincode="411001",
            subtotal=Decimal("199.00"),
            delivery_charge=Decimal("0.00"),
            discount=Decimal("0.00"),
            total=Decimal("199.00"),
        )
        session.add(second_order)
        await session.flush()

        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    Payment(
                        order_id=second_order.id,
                        provider_order_id="order_unique",
                        amount=Decimal("199.00"),
                    )
                )
                await session.flush()

    async def test_amount_must_be_positive(
        self, session: AsyncSession, customer: User
    ):
        from sqlalchemy.exc import IntegrityError

        order = Order(
            user_id=customer.id,
            payment_method=PaymentMethod.ONLINE,
            payment_status=PaymentStatus.CREATED,
            customer_name="Asha Menon",
            customer_email="asha@example.com",
            customer_phone="9876543210",
            address_line1="12 MG Road",
            city="Pune",
            state="Maharashtra",
            pincode="411001",
            subtotal=Decimal("199.00"),
            delivery_charge=Decimal("0.00"),
            discount=Decimal("0.00"),
            total=Decimal("199.00"),
        )
        session.add(order)
        await session.flush()
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(
                    Payment(
                        order_id=order.id,
                        provider_order_id="order_zero",
                        amount=Decimal("0.00"),
                    )
                )
                await session.flush()


class TestSignatureHelpers:
    async def test_checkout_signature_is_hmac_and_rejects_edits(self):
        expected = compute_checkout_signature("order_1", "pay_1", KEY_SECRET)
        assert len(expected) == 64
        assert expected != compute_checkout_signature(
            "order_1", "pay_2", KEY_SECRET
        )

    async def test_webhook_signature_is_over_the_raw_bytes(self):
        body = b'{"event": "payment.captured"}'
        signature = compute_webhook_signature(body, WEBHOOK_SECRET)
        assert signature == compute_webhook_signature(body, WEBHOOK_SECRET)
        assert signature != compute_webhook_signature(
            b'{"event":"payment.captured"}', WEBHOOK_SECRET
        )

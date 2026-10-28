"""Order endpoint tests.

An order moves money, so these tests concentrate on the properties that would be
expensive to get wrong: that the client cannot influence a price, that stock is
only ever decremented alongside a real order, that a failure leaves no trace, and
that one customer can never see or touch another's order.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.order import (
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    PaymentMethod,
)
from app.services.order_service import OrderService
from tests.conftest import _make_user, auth_header_for, make_category, make_product

pytestmark = pytest.mark.asyncio

ORDERS = "/api/v1/orders"
CART = "/api/v1/cart/items"

#: Configured in app/core/config.py; the frontend hardcodes the same two numbers.
DELIVERY_CHARGE = Decimal("99.00")
FREE_DELIVERY_THRESHOLD = Decimal("499.00")

ADDRESS = {
    "line1": "12 MG Road",
    "line2": "Near Metro Station",
    "city": "Pune",
    "state": "Maharashtra",
    "pincode": "411001",
}


def order_body(**overrides) -> dict:
    body = {
        "name": "Asha Menon",
        "email": "asha@example.com",
        "phone": "9876543210",
        "address": dict(ADDRESS),
        "payment_method": "cod",
    }
    body.update(overrides)
    return body


def data(response) -> dict:
    return response.json()["data"]


async def seed_product(session: AsyncSession, **kwargs):
    category = await make_category(session, slug="cakes")
    return await make_product(session, category, **kwargs)


async def add_to_cart(client, headers, product, quantity=1):
    response = await client.post(
        CART,
        json={"product_id": str(product.id), "quantity": quantity},
        headers=headers,
    )
    assert response.status_code == 201, response.text


async def stock_of(session: AsyncSession, product) -> int:
    await session.refresh(product)
    return product.stock_quantity


async def count_orders(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(Order)))


async def order_rows(session: AsyncSession, order_id: uuid.UUID) -> Order:
    """Re-read an order with its lines and products loaded."""
    result = await session.execute(
        select(Order)
        .options(selectinload(Order.items).selectinload(OrderItem.product))
        .where(Order.id == order_id)
    )
    return result.scalar_one()


async def history_rows(session: AsyncSession, order_id: uuid.UUID) -> list[OrderStatusHistory]:
    result = await session.execute(
        select(OrderStatusHistory)
        .where(OrderStatusHistory.order_id == order_id)
        .order_by(OrderStatusHistory.created_at.asc(), OrderStatusHistory.id.asc())
    )
    return list(result.scalars().all())


async def advance(session: AsyncSession, order: Order, *targets: OrderStatus) -> Order:
    """Walk an order forward through the legal transitions."""
    service = OrderService(session)
    for target in targets:
        order = await service.advance(order, target)
    return order


class TestAuth:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", ORDERS),
            ("post", ORDERS),
            ("get", f"{ORDERS}/{uuid.uuid4()}"),
            ("post", f"{ORDERS}/{uuid.uuid4()}/cancel"),
        ],
    )
    async def test_every_route_requires_authentication(self, client: AsyncClient, method, path):
        # httpx's .get() does not accept a json= argument; request() does.
        response = await client.request(
            method.upper(), path, json={} if method == "post" else None
        )
        assert response.status_code == 401, response.text

    async def test_anonymous_cannot_place_an_order(self, client: AsyncClient):
        response = await client.post(ORDERS, json=order_body())
        assert response.status_code == 401


class TestPlace:
    async def test_place_returns_the_full_order(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, name="Brownie", price=Decimal("199.00"))
        response = await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": 2}]),
            headers=customer_headers,
        )
        assert response.status_code == 201, response.text
        body = data(response)
        assert Decimal(body["subtotal"]) == Decimal("398.00")
        assert Decimal(body["delivery_charge"]) == DELIVERY_CHARGE
        assert Decimal(body["total"]) == Decimal("497.00")
        assert body["status"] == "pending"
        assert body["payment_method"] == "cod"
        assert body["customer_name"] == "Asha Menon"
        assert body["line_count"] == 1
        assert body["item_count"] == 2
        assert len(body["items"]) == 1
        assert Decimal(body["items"][0]["line_total"]) == Decimal("398.00")

    async def test_the_address_string_is_produced_by_the_server(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        body = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                headers=customer_headers,
            )
        )
        # The client used to build this string itself at order.service.ts:60.
        assert body["address"] == "12 MG Road, Near Metro Station, Pune, Maharashtra, 411001"
        assert body["shipping_address"]["pincode"] == "411001"

    async def test_stock_is_decremented(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=10)
        await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": 3}]),
            headers=customer_headers,
        )
        assert await stock_of(session, product) == 7

    async def test_a_placed_order_records_its_creation(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        order_id = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                headers=customer_headers,
            )
        )["id"]

        history = await history_rows(session, uuid.UUID(order_id))
        assert len(history) == 1
        assert history[0].to_status is OrderStatus.PENDING
        assert history[0].from_status is None


class TestPricing:
    async def test_the_client_cannot_set_a_price(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """A tampered body must not change what is charged."""
        product = await seed_product(session, price=Decimal("199.00"))
        body = order_body(
            items=[{"product_id": str(product.id), "quantity": 2}],
            subtotal="0.01",
            delivery_charge="0.00",
            discount="0.00",
            total="0.02",
        )
        result = data(
            await client.post(ORDERS, json=body, headers=customer_headers)
        )
        assert Decimal(result["subtotal"]) == Decimal("398.00")
        assert Decimal(result["delivery_charge"]) == DELIVERY_CHARGE
        assert Decimal(result["total"]) == Decimal("497.00")

    async def test_delivery_is_charged_below_the_threshold(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, price=Decimal("199.00"))
        result = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                headers=customer_headers,
            )
        )
        assert Decimal(result["subtotal"]) == Decimal("199.00")
        assert Decimal(result["delivery_charge"]) == DELIVERY_CHARGE
        assert result["free_delivery_applied"] is False
        assert Decimal(result["total"]) == Decimal("298.00")

    async def test_delivery_is_free_at_the_threshold(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """The frontend's own boundary: 499 is the free-delivery threshold."""
        product = await seed_product(session, price=Decimal("499.00"))
        result = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                headers=customer_headers,
            )
        )
        assert Decimal(result["delivery_charge"]) == Decimal("0.00")
        assert result["free_delivery_applied"] is True
        assert Decimal(result["total"]) == Decimal("499.00")

    async def test_a_cent_below_the_threshold_still_pays(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, price=Decimal("498.99"))
        result = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                headers=customer_headers,
            )
        )
        assert Decimal(result["delivery_charge"]) == DELIVERY_CHARGE

    async def test_the_total_always_equals_its_parts(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """Whatever the prices, the stored numbers have to add up."""
        category = await make_category(session, slug="cakes")
        first = await make_product(session, category, price=Decimal("33.33"))
        second = await make_product(session, category, price=Decimal("7.77"))
        result = data(
            await client.post(
                ORDERS,
                json=order_body(items=[
                    {"product_id": str(first.id), "quantity": 3},
                    {"product_id": str(second.id), "quantity": 4},
                ]),
                headers=customer_headers,
            )
        )
        assert Decimal(result["subtotal"]) == Decimal("131.07")
        assert (
            Decimal(result["subtotal"]) + Decimal(result["delivery_charge"])
            - Decimal(result["discount"])
            == Decimal(result["total"])
        )


class TestStock:
    async def test_buying_more_than_is_in_stock_is_refused(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=2)
        response = await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": 5}]),
            headers=customer_headers,
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "insufficient_stock"
        assert response.json()["details"]["available"] == 2
        assert response.json()["details"]["requested"] == 5

    async def test_a_refused_order_leaves_no_trace(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """A failed checkout must not decrement stock or leave a half-order."""
        product = await seed_product(session, stock_quantity=2)
        before = await count_orders(session)
        await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": 5}]),
            headers=customer_headers,
        )
        assert await stock_of(session, product) == 2, "stock must be untouched"
        assert await count_orders(session) == before, "no order may be persisted"

    async def test_a_multi_line_order_is_all_or_nothing(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """The second line is short of stock, so neither line is bought."""
        category = await make_category(session, slug="cakes")
        fine = await make_product(session, category, stock_quantity=10)
        short = await make_product(session, category, stock_quantity=1)
        response = await client.post(
            ORDERS,
            json=order_body(items=[
                {"product_id": str(fine.id), "quantity": 2},
                {"product_id": str(short.id), "quantity": 4},
            ]),
            headers=customer_headers,
        )
        assert response.status_code == 409
        assert await stock_of(session, fine) == 10, (
            "the first line's stock must be rolled back with the rest"
        )
        assert await stock_of(session, short) == 1
        assert await count_orders(session) == 0

    async def test_an_out_of_stock_product_cannot_be_ordered(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=0)
        response = await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
            headers=customer_headers,
        )
        assert response.status_code == 409

    async def test_a_delisted_product_cannot_be_ordered(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, is_active=False)
        response = await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
            headers=customer_headers,
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "product_not_found"

    async def test_an_unknown_product_cannot_be_ordered(
        self, client: AsyncClient, customer_headers
    ):
        response = await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(uuid.uuid4()), "quantity": 1}]),
            headers=customer_headers,
        )
        assert response.status_code == 404

    async def test_exact_stock_is_allowed(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=3)
        response = await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": 3}]),
            headers=customer_headers,
        )
        assert response.status_code == 201
        assert await stock_of(session, product) == 0


class TestFromCart:
    async def test_omitting_items_buys_the_whole_cart(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, price=Decimal("199.00"), stock_quantity=10)
        await add_to_cart(client, customer_headers, product, 2)

        response = await client.post(ORDERS, json=order_body(), headers=customer_headers)
        assert response.status_code == 201, response.text
        assert data(response)["item_count"] == 2
        assert await stock_of(session, product) == 8

    async def test_placing_an_order_empties_the_cart(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        category = await make_category(session, slug="cakes")
        first = await make_product(session, category)
        second = await make_product(session, category)
        await add_to_cart(client, customer_headers, first)
        await add_to_cart(client, customer_headers, second)

        await client.post(ORDERS, json=order_body(), headers=customer_headers)
        cart = data(await client.get(CART, headers=customer_headers))
        assert cart["is_empty"] is True

    async def test_only_the_ordered_lines_leave_the_cart(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """Ordering one of two things must not silently discard the other."""
        category = await make_category(session, slug="cakes")
        ordered = await make_product(session, category, name="Ordered")
        kept = await make_product(session, category, name="Kept")
        await add_to_cart(client, customer_headers, ordered, 1)
        await add_to_cart(client, customer_headers, kept, 3)

        await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(ordered.id), "quantity": 1}]),
            headers=customer_headers,
        )
        cart = data(await client.get(CART, headers=customer_headers))
        assert [line["product"]["name"] for line in cart["items"]] == ["Kept"]
        assert cart["items"][0]["quantity"] == 3

    async def test_an_empty_cart_is_a_conflict(
        self, client: AsyncClient, customer_headers
    ):
        response = await client.post(ORDERS, json=order_body(), headers=customer_headers)
        assert response.status_code == 409
        assert response.json()["error_code"] == "cart_empty"

    async def test_stock_dropped_after_adding_to_cart_is_caught_at_checkout(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """The cart is a view; availability is only really checked at checkout."""
        product = await seed_product(session, stock_quantity=5)
        await add_to_cart(client, customer_headers, product, 5)
        product.stock_quantity = 1
        await session.flush()

        response = await client.post(ORDERS, json=order_body(), headers=customer_headers)
        assert response.status_code == 409
        assert response.json()["error_code"] == "insufficient_stock"


class TestSnapshots:
    async def test_a_later_price_change_does_not_rewrite_history(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, price=Decimal("199.00"))
        order_id = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 2}]),
                headers=customer_headers,
            )
        )["id"]

        product.price = Decimal("299.00")
        await session.flush()

        body = data(await client.get(f"{ORDERS}/{order_id}", headers=customer_headers))
        assert Decimal(body["subtotal"]) == Decimal("398.00")
        assert Decimal(body["items"][0]["unit_price"]) == Decimal("199.00")
        assert Decimal(body["items"][0]["line_total"]) == Decimal("398.00")

    async def test_a_renamed_product_does_not_rewrite_history(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, name="Original Name")
        order_id = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                headers=customer_headers,
            )
        )["id"]

        product.name = "Renamed Treat"
        await session.flush()

        body = data(await client.get(f"{ORDERS}/{order_id}", headers=customer_headers))
        assert body["items"][0]["product_name"] == "Original Name"
        # The live row is still embedded for rendering.
        assert body["items"][0]["product"]["name"] == "Renamed Treat"


class TestListAndGet:
    async def test_listing_returns_only_my_orders(
        self, client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session)
        mine = await _make_user(session)
        theirs = await _make_user(session)
        my_headers = await auth_header_for(client, mine)
        their_headers = await auth_header_for(client, theirs)

        await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
            headers=my_headers,
        )
        await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
            headers=their_headers,
        )

        body = data(await client.get(ORDERS, headers=my_headers))
        assert body["total"] == 1
        assert len(body["orders"]) == 1
        assert body["orders"][0]["user_id"] == str(mine.id)

    async def test_an_empty_history_is_not_an_error(
        self, client: AsyncClient, customer_headers
    ):
        body = data(await client.get(ORDERS, headers=customer_headers))
        assert body["orders"] == []
        assert body["total"] == 0
        assert body["pages"] == 0

    async def test_listing_is_paginated(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=50)
        for _ in range(3):
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                headers=customer_headers,
            )
        first = data(await client.get(f"{ORDERS}?page=1&limit=2", headers=customer_headers))
        assert len(first["orders"]) == 2
        assert first["total"] == 3
        assert first["pages"] == 2

        second = data(await client.get(f"{ORDERS}?page=2&limit=2", headers=customer_headers))
        assert len(second["orders"]) == 1

    async def test_pagination_bounds_are_validated(
        self, client: AsyncClient, customer_headers
    ):
        assert (await client.get(f"{ORDERS}?page=0", headers=customer_headers)).status_code == 422
        assert (await client.get(f"{ORDERS}?limit=0", headers=customer_headers)).status_code == 422
        assert (await client.get(f"{ORDERS}?limit=500", headers=customer_headers)).status_code == 422

    async def test_get_returns_the_callers_order(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        order_id = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                headers=customer_headers,
            )
        )["id"]
        assert data(await client.get(f"{ORDERS}/{order_id}", headers=customer_headers))["id"] == order_id

    async def test_a_malformed_order_id_is_a_422(
        self, client: AsyncClient, customer_headers
    ):
        response = await client.get(f"{ORDERS}/not-a-uuid", headers=customer_headers)
        assert response.status_code == 422


class TestIsolation:
    async def test_another_user_cannot_read_an_order(
        self, client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session)
        owner = await _make_user(session)
        owner_headers = await auth_header_for(client, owner)
        order_id = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                headers=owner_headers,
            )
        )["id"]

        intruder = await _make_user(session)
        intruder_headers = await auth_header_for(client, intruder)
        response = await client.get(f"{ORDERS}/{order_id}", headers=intruder_headers)
        assert response.status_code == 404, (
            "404 rather than 403: a 403 would confirm the order exists"
        )

    async def test_another_user_cannot_cancel_an_order(
        self, client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session, stock_quantity=10)
        owner = await _make_user(session)
        owner_headers = await auth_header_for(client, owner)
        order_id = data(
            await client.post(
                ORDERS,
                json=order_body(items=[{"product_id": str(product.id), "quantity": 4}]),
                headers=owner_headers,
            )
        )["id"]

        intruder = await _make_user(session)
        intruder_headers = await auth_header_for(client, intruder)
        response = await client.post(
            f"{ORDERS}/{order_id}/cancel", headers=intruder_headers
        )
        assert response.status_code == 404

        body = data(await client.get(f"{ORDERS}/{order_id}", headers=owner_headers))
        assert body["status"] == "pending", "the order must be untouched"
        assert await stock_of(session, product) == 6, "stock must not be released"


class TestCancel:
    async def _place(self, client, headers, product, quantity=2):
        response = await client.post(
            ORDERS,
            json=order_body(items=[{"product_id": str(product.id), "quantity": quantity}]),
            headers=headers,
        )
        assert response.status_code == 201, response.text
        return data(response)["id"]

    async def test_cancelling_releases_stock(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=10)
        order_id = await self._place(client, customer_headers, product, 3)
        assert await stock_of(session, product) == 7

        body = data(
            await client.post(f"{ORDERS}/{order_id}/cancel", headers=customer_headers)
        )
        assert body["status"] == "cancelled"
        assert body["is_cancellable"] is False
        assert body["cancelled_at"] is not None
        assert await stock_of(session, product) == 10

    async def test_cancelling_twice_is_a_conflict(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """A silent success here would hide a double-click that also released
        stock a second time."""
        product = await seed_product(session, stock_quantity=10)
        order_id = await self._place(client, customer_headers, product, 3)
        await client.post(f"{ORDERS}/{order_id}/cancel", headers=customer_headers)

        response = await client.post(f"{ORDERS}/{order_id}/cancel", headers=customer_headers)
        assert response.status_code == 409
        assert response.json()["error_code"] == "order_not_cancellable"
        assert await stock_of(session, product) == 10, (
            "stock must be released exactly once"
        )

    async def test_cancelling_records_the_transition(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        order_id = await self._place(client, customer_headers, product, 1)
        await client.post(f"{ORDERS}/{order_id}/cancel", headers=customer_headers)

        history = await history_rows(session, uuid.UUID(order_id))
        assert [entry.to_status for entry in history] == [
            OrderStatus.PENDING,
            OrderStatus.CANCELLED,
        ]
        assert history[-1].from_status is OrderStatus.PENDING

    async def test_a_shipped_order_can_still_be_cancelled(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=10)
        order_id = uuid.UUID(await self._place(client, customer_headers, product, 2))
        order = await order_rows(session, order_id)
        await advance(session, order, OrderStatus.CONFIRMED, OrderStatus.SHIPPED)

        response = await client.post(
            f"{ORDERS}/{order_id}/cancel", headers=customer_headers
        )
        assert response.status_code == 200
        assert data(response)["status"] == "cancelled"

    async def test_a_delivered_order_cannot_be_cancelled(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=10)
        order_id = uuid.UUID(await self._place(client, customer_headers, product, 2))
        order = await order_rows(session, order_id)
        await advance(
            session, order, OrderStatus.CONFIRMED, OrderStatus.SHIPPED, OrderStatus.DELIVERED
        )

        response = await client.post(
            f"{ORDERS}/{order_id}/cancel", headers=customer_headers
        )
        assert response.status_code == 409
        assert response.json()["details"]["status"] == "delivered"

    async def test_cancelling_an_unknown_order_is_a_404(
        self, client: AsyncClient, customer_headers
    ):
        response = await client.post(
            f"{ORDERS}/{uuid.uuid4()}/cancel", headers=customer_headers
        )
        assert response.status_code == 404


class TestStatusMachineThroughTheApi:
    async def test_the_transition_table_is_enforced_by_the_service(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """Skipping a step is refused even though nothing exposed it yet."""
        product = await seed_product(session)
        order_id = uuid.UUID(
            data(
                await client.post(
                    ORDERS,
                    json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                    headers=customer_headers,
                )
            )["id"]
        )
        order = await order_rows(session, order_id)
        with pytest.raises(Exception) as caught:
            await OrderService(session).advance(order, OrderStatus.DELIVERED)
        assert getattr(caught.value, "error_code", "") == "invalid_status_transition"

    async def test_the_happy_path_walks_every_step(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        order_id = uuid.UUID(
            data(
                await client.post(
                    ORDERS,
                    json=order_body(items=[{"product_id": str(product.id), "quantity": 1}]),
                    headers=customer_headers,
                )
            )["id"]
        )
        order = await order_rows(session, order_id)
        order = await advance(
            session,
            order,
            OrderStatus.CONFIRMED,
            OrderStatus.SHIPPED,
            OrderStatus.DELIVERED,
        )
        assert order.status is OrderStatus.DELIVERED
        assert order.delivered_at is not None
        assert order.is_cancellable is False


class TestDatabaseConstraints:
    """The invariants live in the schema, so a service bug cannot persist a lie."""

    def _order_values(self, customer, **overrides) -> dict:
        values = {
            "user_id": customer.id,
            "status": OrderStatus.PENDING,
            "payment_method": PaymentMethod.COD,
            "customer_name": "Asha Menon",
            "customer_email": "asha@example.com",
            "customer_phone": "9876543210",
            "address_line1": "12 MG Road",
            "address_line2": None,
            "city": "Pune",
            "state": "Maharashtra",
            "pincode": "411001",
            "country": "IN",
            "subtotal": Decimal("398.00"),
            "delivery_charge": Decimal("99.00"),
            "discount": Decimal("0.00"),
            "total": Decimal("497.00"),
        }
        values.update(overrides)
        return values

    async def test_an_inconsistent_total_is_refused(self, session: AsyncSession, customer):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(Order(**self._order_values(customer, total=Decimal("1.00"))))
                await session.flush()

    async def test_a_negative_total_is_refused(self, session: AsyncSession, customer):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(Order(**self._order_values(customer, total=Decimal("-5.00"))))
                await session.flush()

    async def test_a_bad_pincode_is_refused(self, session: AsyncSession, customer):
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(Order(**self._order_values(customer, pincode="12345")))
                await session.flush()

    async def test_the_status_enum_is_closed_in_the_database(
        self, session: AsyncSession
    ):
        """The five values are a native enum, not just an API-level Literal.

        The Angular client looks up a badge class through a total ``Record``
        keyed by status, so a sixth value would render ``undefined`` as a CSS
        class. Casting a bad label is refused by the type itself.
        """
        from sqlalchemy import text

        for label in ("pending", "confirmed", "shipped", "delivered", "cancelled"):
            async with session.begin_nested():
                value = await session.scalar(text(f"SELECT '{label}'::order_status"))
                assert value == label

        # A bad label is a data error, not an integrity error: PostgreSQL
        # reports "invalid input value for enum" and asyncpg raises
        # InvalidTextRepresentationError, which SQLAlchemy surfaces as a plain
        # DBAPIError. What matters is that the database refuses it.
        with pytest.raises(DBAPIError) as excinfo:
            async with session.begin_nested():
                await session.execute(text("SELECT 'refunded'::order_status"))
        assert "order_status" in str(excinfo.value)

    async def test_an_inconsistent_line_total_is_refused(
        self, session: AsyncSession, customer
    ):
        product = await seed_product(session, price=Decimal("199.00"))
        order = Order(**self._order_values(customer))
        order.items.append(
            OrderItem(
                product_id=product.id,
                product_name="Brownie",
                unit_price=Decimal("199.00"),
                quantity=2,
                line_total=Decimal("1.00"),  # not price x quantity
            )
        )
        with pytest.raises(IntegrityError):
            async with session.begin_nested():
                session.add(order)
                await session.flush()

    async def test_a_consistent_order_is_accepted(self, session: AsyncSession, customer):
        """The constraint must not be so strict that valid orders are rejected."""
        order = Order(**self._order_values(customer))
        async with session.begin_nested():
            session.add(order)
            await session.flush()
        assert order.id is not None

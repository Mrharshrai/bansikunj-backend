"""Cart endpoint tests.

The two rules under test throughout: a client never sets a price, and a cart is
invisible to anyone but its owner.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _make_user, auth_header_for, make_category, make_product

pytestmark = pytest.mark.asyncio

CART = "/api/v1/cart/items"


def add_body(product, quantity: int = 1, **extra) -> dict:
    # Accepts a Product instance or a bare id, so the unknown-product case can
    # pass a random UUID without a lookup.
    product_id = getattr(product, "id", product)
    body = {"product_id": str(product_id), "quantity": quantity}
    body.update(extra)
    return body


def data(response) -> dict:
    return response.json()["data"]


def slugs_of_line(line: dict) -> str:
    return line["product"]["slug"]


async def seed_product(session: AsyncSession, **kwargs):
    category = await make_category(session, slug="cakes")
    return await make_product(session, category, **kwargs)


class TestAuth:
    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("get", CART, None),
            ("post", CART, {}),
            ("put", f"{CART}/{uuid.uuid4()}", {"quantity": 1}),
            ("delete", f"{CART}/{uuid.uuid4()}", None),
            ("delete", CART, None),
        ],
    )
    async def test_every_route_requires_authentication(self, client: AsyncClient, method, path, body):
        # httpx's .get()/.delete() do not accept a json= argument; request() does.
        response = await client.request(method.upper(), path, json=body)
        assert response.status_code == 401, response.text
        assert response.json()["error_code"] in {
            "missing_credentials",
            "invalid_token",
            "unauthorized",
        }

    async def test_a_bad_token_is_rejected(self, client: AsyncClient):
        response = await client.get(CART, headers={"Authorization": "Bearer not-a-jwt"})
        assert response.status_code == 401


class TestEmptyCart:
    async def test_a_user_with_no_lines_gets_an_empty_cart(
        self, client: AsyncClient, customer_headers
    ):
        response = await client.get(CART, headers=customer_headers)
        assert response.status_code == 200
        body = data(response)
        assert body["items"] == []
        assert body["is_empty"] is True
        assert body["line_count"] == 0
        assert body["item_count"] == 0
        assert Decimal(body["subtotal"]) == Decimal("0.00")


class TestAdd:
    async def test_add_returns_the_line_with_the_product_embedded(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, name="Brownie", price=Decimal("199.00"))
        response = await client.post(
            CART, json=add_body(product, 2), headers=customer_headers
        )
        assert response.status_code == 201, response.text
        body = data(response)
        assert body["line_count"] == 1
        line = body["items"][0]
        assert line["product_id"] == str(product.id)
        assert line["quantity"] == 2
        assert Decimal(line["line_total"]) == Decimal("398.00")
        assert Decimal(body["subtotal"]) == Decimal("398.00")
        assert line["product"]["name"] == "Brownie"
        assert line["product"]["slug"] == product.slug

    async def test_quantity_defaults_to_one(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        response = await client.post(
            CART, json={"product_id": str(product.id)}, headers=customer_headers
        )
        assert data(response)["items"][0]["quantity"] == 1

    async def test_adding_twice_increments_instead_of_duplicating(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=10)
        await client.post(CART, json=add_body(product, 2), headers=customer_headers)
        response = await client.post(CART, json=add_body(product, 3), headers=customer_headers)
        body = data(response)
        assert body["line_count"] == 1, "a product must occupy at most one line"
        assert body["items"][0]["quantity"] == 5

    async def test_the_client_cannot_set_a_price(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """A tampered price field must not reach the line total."""
        product = await seed_product(session, price=Decimal("199.00"))
        response = await client.post(
            CART,
            json=add_body(product, 2, price="0.01", line_total="0.02", subtotal="0.03"),
            headers=customer_headers,
        )
        assert response.status_code == 201, response.text
        line = data(response)["items"][0]
        assert Decimal(line["line_total"]) == Decimal("398.00")
        assert Decimal(data(response)["subtotal"]) == Decimal("398.00")

    async def test_increment_is_checked_against_the_summed_quantity(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """Each add is within stock, but together they are not."""
        product = await seed_product(session, stock_quantity=5)
        assert (
            await client.post(CART, json=add_body(product, 4), headers=customer_headers)
        ).status_code == 201
        response = await client.post(
            CART, json=add_body(product, 4), headers=customer_headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "insufficient_stock"
        assert response.json()["details"]["available"] == 5

    async def test_increment_cannot_exceed_the_per_line_cap(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=1000)
        await client.post(CART, json=add_body(product, 60), headers=customer_headers)
        response = await client.post(
            CART, json=add_body(product, 60), headers=customer_headers
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "cart_quantity_exceeded"

    async def test_more_than_in_stock_is_refused(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=3)
        # Capture the id first: the refused request rolls back, which expires
        # every instance in the session, and a later attribute read would try to
        # reload it outside an await and raise MissingGreenlet.
        product_id = str(product.id)
        response = await client.post(
            CART, json=add_body(product, 4), headers=customer_headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "insufficient_stock"
        assert response.json()["details"] == {
            "requested": 4,
            "available": 3,
            "product_id": product_id,
        }

    async def test_an_out_of_stock_product_cannot_be_added(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=0)
        response = await client.post(CART, json=add_body(product), headers=customer_headers)
        assert response.status_code == 409

    async def test_a_delisted_product_is_a_404(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, is_active=False)
        response = await client.post(CART, json=add_body(product), headers=customer_headers)
        assert response.status_code == 404
        assert response.json()["error_code"] == "product_not_found"

    async def test_an_unknown_product_is_a_404(
        self, client: AsyncClient, customer_headers
    ):
        response = await client.post(
            CART, json=add_body(uuid.uuid4()), headers=customer_headers
        )
        assert response.status_code == 404

    @pytest.mark.parametrize("quantity", [0, -1, 100, 10_000])
    async def test_out_of_range_quantity_is_rejected(
        self, client: AsyncClient, session: AsyncSession, customer_headers, quantity: int
    ):
        product = await seed_product(session, stock_quantity=10_000)
        response = await client.post(
            CART, json=add_body(product, quantity), headers=customer_headers
        )
        assert response.status_code == 422, response.text

    async def test_a_malformed_product_id_is_rejected(
        self, client: AsyncClient, customer_headers
    ):
        response = await client.post(
            CART, json={"product_id": "not-a-uuid"}, headers=customer_headers
        )
        assert response.status_code == 422


class TestSetQuantity:
    async def _add(self, client, headers, product, quantity=1):
        response = await client.post(
            CART, json=add_body(product, quantity), headers=headers
        )
        assert response.status_code == 201, response.text
        return data(response)

    async def test_quantity_is_absolute_not_a_delta(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=10)
        await self._add(client, customer_headers, product, 2)
        response = await client.put(
            f"{CART}/{product.id}", json={"quantity": 7}, headers=customer_headers
        )
        assert response.status_code == 200
        assert data(response)["items"][0]["quantity"] == 7

    async def test_quantity_can_be_lowered(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=10)
        await self._add(client, customer_headers, product, 8)
        response = await client.put(
            f"{CART}/{product.id}", json={"quantity": 1}, headers=customer_headers
        )
        assert data(response)["items"][0]["quantity"] == 1

    async def test_above_stock_is_refused(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session, stock_quantity=4)
        await self._add(client, customer_headers, product)
        response = await client.put(
            f"{CART}/{product.id}", json={"quantity": 5}, headers=customer_headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "insufficient_stock"

    async def test_a_line_that_is_not_in_the_cart_is_a_404(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        response = await client.put(
            f"{CART}/{product.id}", json={"quantity": 2}, headers=customer_headers
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "cart_item_not_found"

    async def test_zero_quantity_is_rejected_rather_than_deleting(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        await self._add(client, customer_headers, product)
        response = await client.put(
            f"{CART}/{product.id}", json={"quantity": 0}, headers=customer_headers
        )
        assert response.status_code == 422

    async def test_a_delisted_product_cannot_have_its_quantity_raised(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        await self._add(client, customer_headers, product)
        product.is_active = False
        await session.flush()
        response = await client.put(
            f"{CART}/{product.id}", json={"quantity": 3}, headers=customer_headers
        )
        assert response.status_code == 404


class TestRemove:
    async def test_remove_drops_the_line(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        await client.post(CART, json=add_body(product), headers=customer_headers)
        response = await client.delete(f"{CART}/{product.id}", headers=customer_headers)
        assert response.status_code == 200
        body = data(response)
        assert body["items"] == []
        assert body["is_empty"] is True

    async def test_remove_is_idempotent(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        await client.post(CART, json=add_body(product), headers=customer_headers)
        first = await client.delete(f"{CART}/{product.id}", headers=customer_headers)
        second = await client.delete(f"{CART}/{product.id}", headers=customer_headers)
        assert first.status_code == second.status_code == 200

    async def test_removing_something_never_added_succeeds(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        product = await seed_product(session)
        response = await client.delete(f"{CART}/{product.id}", headers=customer_headers)
        assert response.status_code == 200

    async def test_clear_empties_the_whole_cart(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        category = await make_category(session, slug="cakes")
        first = await make_product(session, category)
        second = await make_product(session, category)
        await client.post(CART, json=add_body(first), headers=customer_headers)
        await client.post(CART, json=add_body(second), headers=customer_headers)

        response = await client.delete(CART, headers=customer_headers)
        assert response.status_code == 200
        assert data(response)["items"] == []
        assert (await client.get(CART, headers=customer_headers)).json()["data"]["line_count"] == 0


class TestTotals:
    async def test_multiple_lines_are_totalled_correctly(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        category = await make_category(session, slug="cakes")
        brownie = await make_product(session, category, price=Decimal("199.99"))
        cookie = await make_product(session, category, price=Decimal("50.00"))

        await client.post(CART, json=add_body(brownie, 3), headers=customer_headers)
        response = await client.post(
            CART, json=add_body(cookie, 2), headers=customer_headers
        )
        body = data(response)
        assert body["line_count"] == 2
        assert body["item_count"] == 5, "item_count sums quantities, line_count does not"
        # 199.99 * 3 = 599.97 exactly; a float would drift here.
        assert Decimal(body["subtotal"]) == Decimal("699.97")
        assert {slugs_of_line(line) for line in body["items"]} == {
            brownie.slug,
            cookie.slug,
        }

    async def test_line_total_tracks_a_price_change(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """Prices are computed on read, so a cart cannot hold a stale total."""
        product = await seed_product(session, price=Decimal("100.00"))
        await client.post(CART, json=add_body(product, 2), headers=customer_headers)

        product.price = Decimal("150.00")
        await session.flush()

        line = data(await client.get(CART, headers=customer_headers))["items"][0]
        assert Decimal(line["line_total"]) == Decimal("300.00")

    async def test_subtotal_has_no_delivery_charge(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """Delivery depends on the address, so it belongs to order placement."""
        product = await seed_product(session, price=Decimal("100.00"))
        body = data(await client.post(CART, json=add_body(product), headers=customer_headers))
        assert set(body) == {
            "items",
            "line_count",
            "item_count",
            "subtotal",
            "is_empty",
        }


class TestIsolation:
    """A cart is private. These are the tests that matter most."""

    async def test_a_cart_is_invisible_to_another_user(
        self, client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session)
        owner = await _make_user(session)
        owner_headers = await auth_header_for(client, owner)
        await client.post(CART, json=add_body(product, 4), headers=owner_headers)

        intruder = await _make_user(session)
        intruder_headers = await auth_header_for(client, intruder)
        body = data(await client.get(CART, headers=intruder_headers))
        assert body["items"] == [], "one user's cart must never leak into another's"

    async def test_another_user_cannot_change_the_quantity(
        self, client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session, stock_quantity=50)
        owner = await _make_user(session)
        owner_headers = await auth_header_for(client, owner)
        await client.post(CART, json=add_body(product, 2), headers=owner_headers)

        intruder = await _make_user(session)
        intruder_headers = await auth_header_for(client, intruder)
        response = await client.put(
            f"{CART}/{product.id}", json={"quantity": 40}, headers=intruder_headers
        )
        assert response.status_code == 404, "must be indistinguishable from 'not in cart'"

        line = data(await client.get(CART, headers=owner_headers))["items"][0]
        assert line["quantity"] == 2, "the owner's line must be untouched"

    async def test_another_user_cannot_remove_a_line(
        self, client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session)
        owner = await _make_user(session)
        owner_headers = await auth_header_for(client, owner)
        await client.post(CART, json=add_body(product), headers=owner_headers)

        intruder = await _make_user(session)
        intruder_headers = await auth_header_for(client, intruder)
        await client.delete(f"{CART}/{product.id}", headers=intruder_headers)

        assert len(data(await client.get(CART, headers=owner_headers))["items"]) == 1

    async def test_clearing_does_not_touch_another_users_cart(
        self, client: AsyncClient, session: AsyncSession
    ):
        product = await seed_product(session)
        owner = await _make_user(session)
        owner_headers = await auth_header_for(client, owner)
        await client.post(CART, json=add_body(product), headers=owner_headers)

        intruder = await _make_user(session)
        intruder_headers = await auth_header_for(client, intruder)
        await client.delete(CART, headers=intruder_headers)

        assert len(data(await client.get(CART, headers=owner_headers))["items"]) == 1


class TestDelistedProductAlreadyInCart:
    async def test_a_delisted_line_stays_visible(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        """The line is kept so the shopper can see why checkout will fail.

        Order placement is where availability is re-validated; silently dropping
        the line here would make the cart disagree with itself.
        """
        product = await seed_product(session, name="Retired Treat")
        await client.post(CART, json=add_body(product), headers=customer_headers)
        product.is_active = False
        await session.flush()

        body = data(await client.get(CART, headers=customer_headers))
        assert len(body["items"]) == 1
        assert body["items"][0]["product"]["name"] == "Retired Treat"
        assert body["items"][0]["product"]["is_active"] is False

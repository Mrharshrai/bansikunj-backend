"""Product endpoint tests: CRUD, filtering, sorting, pagination, visibility."""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Role
from tests.conftest import (
    _make_user,
    auth_header_for,
    make_category,
    make_product,
    product_payload,
)

pytestmark = pytest.mark.asyncio


def slugs(response) -> list[str]:
    return [p["slug"] for p in response.json()["data"]["products"]]


async def seed(session: AsyncSession) -> dict[str, str]:
    """A small catalogue with predictable prices."""
    cakes = await make_category(session, name="Cakes", slug="cakes")
    cookies = await make_category(session, name="Cookies", slug="cookies")
    return {
        "chocolate": (
            await make_product(
                session,
                cookies,
                name="Belgian Dark Chunk",
                price=Decimal("249.00"),
                mrp=Decimal("299.00"),
                tags=["chocolate", "belgian"],
                is_best_seller=True,
                review_count=50,
                rating=Decimal("4.7"),
                stock_quantity=5,
                slug="chocolate",
            )
        ).slug,
        "vanilla": (
            await make_product(
                session,
                cookies,
                name="Vanilla Dream",
                price=Decimal("149.00"),
                tags=["vanilla"],
                review_count=10,
                stock_quantity=0,
                slug="vanilla",
            )
        ).slug,
        "carrot": (
            await make_product(
                session,
                cakes,
                name="Carrot Halwa Cake",
                price=Decimal("399.00"),
                mrp=Decimal("499.00"),
                tags=["nuts"],
                review_count=30,
                stock_quantity=2,
                slug="carrot",
            )
        ).slug,
    }


class TestPublicListing:
    async def test_list_is_public_and_paginated(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        response = await client.get("/api/v1/products")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 3
        assert data["page"] == 1
        assert data["limit"] == 20
        assert data["pages"] == 1
        assert len(data["products"]) == 3

    async def test_products_carry_category_and_derived_fields(
        self, client: AsyncClient, session: AsyncSession
    ):
        await seed(session)
        product = next(
            p
            for p in (await client.get("/api/v1/products")).json()["data"]["products"]
            if p["slug"] == "chocolate"
        )
        assert product["category"] == "cookies"
        assert product["category_name"] == "Cookies"
        assert product["in_stock"] is True
        assert product["discount_percent"] == 17
        assert product["is_best_seller"] is True
        assert Decimal(product["rating"]) == Decimal("4.7")
        assert product["tags"] == ["chocolate", "belgian"]

    async def test_no_discount_yields_null(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        product = next(
            p
            for p in (await client.get("/api/v1/products")).json()["data"]["products"]
            if p["slug"] == "vanilla"
        )
        assert product["mrp"] is None
        assert product["discount_percent"] is None

    async def test_out_of_stock_is_flagged(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        product = next(
            p
            for p in (await client.get("/api/v1/products")).json()["data"]["products"]
            if p["slug"] == "vanilla"
        )
        assert product["stock_quantity"] == 0
        assert product["in_stock"] is False

    async def test_inactive_products_are_hidden(self, client: AsyncClient, session: AsyncSession):
        cat = await make_category(session, slug="cakes")
        await make_product(session, cat, name="Visible")
        await make_product(session, cat, name="Delisted", is_active=False)
        assert (await client.get("/api/v1/products")).json()["data"]["total"] == 1

    async def test_search_matches_name(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        response = await client.get("/api/v1/products", params={"q": "vanilla"})
        assert slugs(response) == ["vanilla"]

    async def test_search_matches_description(self, client: AsyncClient, session: AsyncSession):
        cat = await make_category(session, slug="cakes")
        await make_product(session, cat, name="Plain", description="contains cardamom")
        response = await client.get("/api/v1/products", params={"q": "cardamom"})
        assert response.json()["data"]["total"] == 1

    async def test_search_matches_a_tag(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        assert slugs(await client.get("/api/v1/products", params={"q": "belgian"})) == [
            "chocolate"
        ]

    async def test_search_is_case_insensitive(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        assert slugs(await client.get("/api/v1/products", params={"q": "BELGIAN"})) == [
            "chocolate"
        ]

    async def test_search_treats_percent_as_literal(self, client: AsyncClient, session: AsyncSession):
        cat = await make_category(session, slug="cakes")
        await make_product(session, cat, name="Fifty", description="50% cocoa")
        await make_product(session, cat, name="Other", description="nothing special")
        response = await client.get("/api/v1/products", params={"q": "50%"})
        assert response.json()["data"]["total"] == 1

    async def test_filter_by_category(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        assert sorted(slugs(await client.get("/api/v1/products", params={"category": "cookies"}))) == [
            "chocolate",
            "vanilla",
        ]

    async def test_filter_by_tag(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        assert slugs(await client.get("/api/v1/products", params={"tag": "nuts"})) == ["carrot"]

    async def test_filter_by_price_range(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        response = await client.get(
            "/api/v1/products", params={"min_price": "150", "max_price": "300"}
        )
        assert slugs(response) == ["chocolate"]

    async def test_filter_by_best_seller(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        assert slugs(
            await client.get("/api/v1/products", params={"is_best_seller": True})
        ) == ["chocolate"]

    async def test_filter_by_stock(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        assert slugs(await client.get("/api/v1/products", params={"in_stock": False})) == [
            "vanilla"
        ]

    async def test_combined_filters(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        response = await client.get(
            "/api/v1/products", params={"category": "cookies", "in_stock": True}
        )
        assert slugs(response) == ["chocolate"]

    async def test_filters_are_echoed_back(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        response = await client.get("/api/v1/products", params={"category": "cookies"})
        assert response.json()["data"]["filters"] == {"category": "cookies"}

    async def test_unknown_category_filter_returns_empty(
        self, client: AsyncClient, session: AsyncSession
    ):
        await seed(session)
        response = await client.get("/api/v1/products", params={"category": "nope"})
        assert response.json()["data"]["total"] == 0


class TestSorting:
    async def test_price_asc_and_desc(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        assert slugs(await client.get("/api/v1/products", params={"sort": "price-asc"})) == [
            "vanilla",
            "chocolate",
            "carrot",
        ]
        assert slugs(await client.get("/api/v1/products", params={"sort": "price-desc"})) == [
            "carrot",
            "chocolate",
            "vanilla",
        ]

    async def test_popular_orders_by_reviews(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        assert slugs(await client.get("/api/v1/products", params={"sort": "popular"})) == [
            "chocolate",
            "carrot",
            "vanilla",
        ]

    async def test_rating_sort(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        assert slugs(await client.get("/api/v1/products", params={"sort": "rating"}))[0] == (
            "chocolate"
        )

    async def test_invalid_sort_returns_422(self, client: AsyncClient):
        response = await client.get("/api/v1/products", params={"sort": "; DROP TABLE users"})
        assert response.status_code == 422

    async def test_min_price_greater_than_max_returns_422(self, client: AsyncClient):
        response = await client.get(
            "/api/v1/products", params={"min_price": "500", "max_price": "100"}
        )
        assert response.status_code == 422

    async def test_non_numeric_price_returns_422(self, client: AsyncClient):
        response = await client.get("/api/v1/products", params={"min_price": "abc"})
        assert response.status_code == 422
        assert response.json()["error_code"] == "validation_error"


class TestPagination:
    async def test_pages_do_not_overlap(self, client: AsyncClient, session: AsyncSession):
        cat = await make_category(session, slug="cakes")
        for i in range(5):
            await make_product(session, cat, name=f"Cake {i}", price=Decimal(100 + i))

        first = await client.get("/api/v1/products", params={"page": 1, "limit": 2, "sort": "price-asc"})
        second = await client.get("/api/v1/products", params={"page": 2, "limit": 2, "sort": "price-asc"})
        third = await client.get("/api/v1/products", params={"page": 3, "limit": 2, "sort": "price-asc"})

        assert first.json()["data"]["pages"] == 3
        assert len(first.json()["data"]["products"]) == 2
        assert len(third.json()["data"]["products"]) == 1
        seen = slugs(first) + slugs(second) + slugs(third)
        assert len(set(seen)) == 5, "pagination must not repeat or skip products"

    async def test_page_beyond_the_end_is_empty(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        response = await client.get("/api/v1/products", params={"page": 99})
        assert response.status_code == 200
        assert response.json()["data"]["products"] == []

    @pytest.mark.parametrize("params", [{"page": 0}, {"limit": 0}, {"limit": 101}])
    async def test_out_of_range_paging_returns_422(self, client: AsyncClient, params: dict):
        assert (await client.get("/api/v1/products", params=params)).status_code == 422


class TestDetail:
    async def test_fetch_by_slug(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        response = await client.get("/api/v1/products/chocolate")
        assert response.status_code == 200
        assert response.json()["data"]["product"]["name"] == "Belgian Dark Chunk"

    async def test_fetch_by_uuid(self, client: AsyncClient, session: AsyncSession, category):
        created = await make_product(session, category, name="By Id")
        response = await client.get(f"/api/v1/products/{created.id}")
        assert response.status_code == 200
        assert response.json()["data"]["product"]["id"] == str(created.id)

    async def test_unknown_reference_returns_404(self, client: AsyncClient):
        response = await client.get("/api/v1/products/definitely-not-here")
        assert response.status_code == 404
        assert response.json()["error_code"] == "product_not_found"

    async def test_unknown_uuid_returns_404(self, client: AsyncClient):
        assert (await client.get(f"/api/v1/products/{uuid.uuid4()}")).status_code == 404

    async def test_inactive_product_is_404(self, client: AsyncClient, session: AsyncSession, category):
        await make_product(session, category, name="Gone", is_active=False)
        assert (await client.get("/api/v1/products/gone")).status_code == 404

    async def test_tags_route_is_not_shadowed_by_the_detail_route(
        self, client: AsyncClient, session: AsyncSession
    ):
        """A product literally slugged 'tags' must not hijack /products/tags."""
        cat = await make_category(session, slug="cakes")
        await make_product(session, cat, name="Taggy", tags=["sweet"])
        response = await client.get("/api/v1/products/tags")
        assert response.status_code == 200
        assert response.json()["data"]["tags"] == ["sweet"]

    async def test_tags_list(self, client: AsyncClient, session: AsyncSession):
        await seed(session)
        response = await client.get("/api/v1/products/tags")
        assert response.json()["data"]["tags"] == ["belgian", "chocolate", "nuts", "vanilla"]


class TestAdminCreate:
    async def test_admin_creates_a_product(self, client: AsyncClient, admin_headers, category):
        response = await client.post(
            "/api/v1/products", json=product_payload("soft-chewy"), headers=admin_headers
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]["product"]
        assert data["slug"] == "belgian-dark-chunk"
        assert data["category"] == "soft-chewy"
        assert Decimal(data["price"]) == Decimal("249.00")
        assert data["in_stock"] is True
        assert data["rating"] is None
        assert data["review_count"] == 0

    async def test_tags_are_normalised(self, client: AsyncClient, admin_headers, category):
        response = await client.post(
            "/api/v1/products",
            json=product_payload("soft-chewy", tags=[" Chocolate ", "chocolate", "NUTS"]),
            headers=admin_headers,
        )
        assert response.json()["data"]["product"]["tags"] == ["chocolate", "nuts"]

    async def test_duplicate_name_gets_a_suffixed_slug(
        self, client: AsyncClient, admin_headers, category
    ):
        await client.post("/api/v1/products", json=product_payload("soft-chewy"), headers=admin_headers)
        second = await client.post(
            "/api/v1/products", json=product_payload("soft-chewy"), headers=admin_headers
        )
        assert second.status_code == 201
        assert second.json()["data"]["product"]["slug"] == "belgian-dark-chunk-2"

    async def test_unknown_category_returns_404(
        self, client: AsyncClient, admin_headers
    ):
        response = await client.post(
            "/api/v1/products", json=product_payload("nope"), headers=admin_headers
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "category_not_found"

    async def test_inactive_category_is_rejected(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        await make_category(session, slug="retired", is_active=False)
        response = await client.post(
            "/api/v1/products", json=product_payload("retired"), headers=admin_headers
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "category_inactive"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"price": "0"},
            {"price": "-5"},
            {"mrp": "100", "price": "200"},
            {"stock_quantity": -1},
            {"name": "  "},
            {"description": ""},
            {"image_url": "javascript:alert(1)"},
            {"slug": "Not A Slug"},
        ],
    )
    async def test_invalid_payloads_return_422(
        self, client: AsyncClient, admin_headers, category, overrides: dict
    ):
        response = await client.post(
            "/api/v1/products",
            json=product_payload("soft-chewy", **overrides),
            headers=admin_headers,
        )
        assert response.status_code == 422, f"{overrides} -> {response.text}"

    async def test_requires_authentication(self, client: AsyncClient, category):
        assert (
            await client.post("/api/v1/products", json=product_payload("soft-chewy"))
        ).status_code == 401

    async def test_customer_is_forbidden(self, client: AsyncClient, session: AsyncSession, category):
        user = await _make_user(session)
        headers = await auth_header_for(client, user)
        response = await client.post(
            "/api/v1/products", json=product_payload("soft-chewy"), headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error_code"] == "insufficient_role"


class TestAdminUpdate:
    async def _create(self, client, headers, category_slug="soft-chewy") -> dict:
        response = await client.post(
            "/api/v1/products", json=product_payload(category_slug), headers=headers
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]["product"]

    async def test_patch_updates_only_given_fields(
        self, client: AsyncClient, admin_headers, category
    ):
        created = await self._create(client, admin_headers)
        response = await client.patch(
            f"/api/v1/products/{created['slug']}",
            json={"price": "199.00"},
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()["data"]["product"]
        assert Decimal(data["price"]) == Decimal("199.00")
        assert data["name"] == created["name"]
        assert data["description"] == created["description"]
        assert Decimal(data["mrp"]) == Decimal("299.00")

    async def test_raising_price_above_existing_mrp_is_rejected(
        self, client: AsyncClient, admin_headers, category
    ):
        """The service combines the incoming price with the stored mrp.

        Marking a product down is the normal admin action and always allowed --
        price below mrp is just a deeper discount. What must be refused is a
        price that would end up *above* the stored mrp, and that can only be
        detected if the stored value is combined with the incoming one.
        """
        created = await self._create(client, admin_headers)  # price 249, mrp 299
        response = await client.patch(
            f"/api/v1/products/{created['slug']}",
            json={"price": "350.00"},
            headers=admin_headers,
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "mrp_below_price"

    async def test_marking_a_product_down_keeps_the_stored_mrp(
        self, client: AsyncClient, admin_headers, category
    ):
        """Lowering price alone is allowed and leaves mrp untouched."""
        created = await self._create(client, admin_headers)  # price 249, mrp 299
        response = await client.patch(
            f"/api/v1/products/{created['slug']}",
            json={"price": "100.00"},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]["product"]
        assert Decimal(data["price"]) == Decimal("100.00")
        assert Decimal(data["mrp"]) == Decimal("299.00")

    async def test_lowering_price_and_mrp_together_is_allowed(
        self, client: AsyncClient, admin_headers, category
    ):
        created = await self._create(client, admin_headers)
        response = await client.patch(
            f"/api/v1/products/{created['slug']}",
            json={"price": "100.00", "mrp": "120.00"},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert Decimal(response.json()["data"]["product"]["price"]) == Decimal("100.00")

    async def test_moving_to_another_category(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        other = await make_category(session, name="Cakes", slug="cakes")
        created = await self._create(client, admin_headers)
        response = await client.patch(
            f"/api/v1/products/{created['slug']}",
            json={"category": other.slug},
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()["data"]["product"]
        assert data["category"] == "cakes"
        assert data["category_name"] == "Cakes"

    async def test_put_replaces_optional_fields(
        self, client: AsyncClient, admin_headers, category
    ):
        created = await self._create(client, admin_headers)
        body = product_payload("soft-chewy")
        body.pop("mrp")
        body["name"] = "Replaced"
        response = await client.put(
            f"/api/v1/products/{created['slug']}", json=body, headers=admin_headers
        )
        assert response.status_code == 200
        data = response.json()["data"]["product"]
        assert data["name"] == "Replaced"
        assert data["mrp"] is None

    async def test_toggling_stock_and_best_seller(
        self, client: AsyncClient, admin_headers, category
    ):
        created = await self._create(client, admin_headers)
        response = await client.patch(
            f"/api/v1/products/{created['slug']}",
            json={"stock_quantity": 0, "is_best_seller": True},
            headers=admin_headers,
        )
        data = response.json()["data"]["product"]
        assert data["in_stock"] is False
        assert data["is_best_seller"] is True

    async def test_unknown_product_returns_404(self, client: AsyncClient, admin_headers):
        response = await client.patch(
            f"/api/v1/products/{uuid.uuid4()}", json={"price": "10"}, headers=admin_headers
        )
        assert response.status_code == 404

    async def test_customer_is_forbidden(self, client: AsyncClient, session: AsyncSession, category):
        created = await self._create(
            client, await auth_header_for(client, await _make_user(session, role=Role.ADMIN))
        )
        user = await _make_user(session)
        headers = await auth_header_for(client, user)
        response = await client.patch(
            f"/api/v1/products/{created['slug']}", json={"price": "1"}, headers=headers
        )
        assert response.status_code == 403


class TestAdminDelete:
    async def test_delete_delists_rather_than_removes(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        cat = await make_category(session, slug="cakes")
        product = await make_product(session, cat, name="Doomed")
        response = await client.delete(
            f"/api/v1/products/{product.slug}", headers=admin_headers
        )
        assert response.status_code == 200
        assert (await client.get(f"/api/v1/products/{product.slug}")).status_code == 404
        assert (await client.get("/api/v1/products")).json()["data"]["total"] == 0

    async def test_delete_is_idempotent(self, client: AsyncClient, session: AsyncSession, admin_headers):
        cat = await make_category(session, slug="cakes")
        product = await make_product(session, cat, name="Doomed")
        first = await client.delete(f"/api/v1/products/{product.slug}", headers=admin_headers)
        second = await client.delete(f"/api/v1/products/{product.slug}", headers=admin_headers)
        assert first.status_code == second.status_code == 200

    async def test_delete_requires_authentication(self, client: AsyncClient, session: AsyncSession):
        cat = await make_category(session, slug="cakes")
        product = await make_product(session, cat, name="Doomed")
        assert (await client.delete(f"/api/v1/products/{product.slug}")).status_code == 401

    async def test_customer_cannot_delete(self, client: AsyncClient, session: AsyncSession):
        cat = await make_category(session, slug="cakes")
        product = await make_product(session, cat, name="Safe")
        user = await _make_user(session)
        headers = await auth_header_for(client, user)
        assert (
            await client.delete(f"/api/v1/products/{product.slug}", headers=headers)
        ).status_code == 403
        assert (await client.get(f"/api/v1/products/{product.slug}")).status_code == 200


class TestStockAdjustment:
    """``PATCH /products/{reference}/stock`` changes quantity and nothing else."""

    async def test_delta_increases_stock(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        product = await make_product(session, category, stock_quantity=10)
        response = await client.patch(
            f"/api/v1/products/{product.slug}/stock",
            json={"delta": 25, "reason": "monday delivery"},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["previous_quantity"] == 10
        assert data["new_quantity"] == 35
        assert data["delta"] == 25
        assert data["reason"] == "monday delivery"
        assert data["product"]["stock_quantity"] == 35
        assert data["product"]["in_stock"] is True

    async def test_absolute_quantity_sets_the_count(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        product = await make_product(session, category, stock_quantity=99)
        response = await client.patch(
            f"/api/v1/products/{product.id}/stock",
            json={"quantity": 4},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["new_quantity"] == 4

    async def test_other_fields_are_untouched(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        """The point of a dedicated endpoint: stock edits cannot clobber a price."""
        product = await make_product(
            session, category, name="Belgian Dark Chunk", price=Decimal("249.00"),
            stock_quantity=10, tags=["chocolate"],
        )
        await client.patch(
            f"/api/v1/products/{product.slug}/stock",
            json={"delta": 5},
            headers=admin_headers,
        )
        stored = (await client.get(f"/api/v1/products/{product.slug}")).json()["data"]["product"]
        assert stored["name"] == "Belgian Dark Chunk"
        assert Decimal(stored["price"]) == Decimal("249.00")
        assert stored["tags"] == ["chocolate"]
        assert stored["stock_quantity"] == 15

    async def test_stock_can_be_drained_to_zero(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        product = await make_product(session, category, stock_quantity=3)
        response = await client.patch(
            f"/api/v1/products/{product.slug}/stock",
            json={"delta": -3},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["product"]["in_stock"] is False

    async def test_oversell_is_409_and_leaves_stock_intact(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        product = await make_product(session, category, stock_quantity=4)
        # Read the slug up front: a rejected adjustment rolls back, which expires
        # the ORM instance, and a later attribute access would try a lazy load
        # outside the session's greenlet.
        slug = product.slug
        response = await client.patch(
            f"/api/v1/products/{slug}/stock",
            json={"delta": -10},
            headers=admin_headers,
        )
        assert response.status_code == 409
        body = response.json()
        assert body["error_code"] == "insufficient_stock"
        assert body["details"]["shortfall"] == 6
        stored = (await client.get(f"/api/v1/products/{slug}")).json()["data"]["product"]
        assert stored["stock_quantity"] == 4

    async def test_unknown_product_returns_404(
        self, client: AsyncClient, admin_headers
    ):
        response = await client.patch(
            f"/api/v1/products/{uuid.uuid4()}/stock",
            json={"delta": 1},
            headers=admin_headers,
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "product_not_found"

    async def test_delisted_product_can_still_be_restocked(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        """Clearing a shelf should not strand the remaining units forever."""
        product = await make_product(session, category, stock_quantity=2, is_active=False)
        response = await client.patch(
            f"/api/v1/products/{product.slug}/stock",
            json={"delta": 40},
            headers=admin_headers,
        )
        assert response.status_code == 200
        assert response.json()["data"]["new_quantity"] == 42

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"delta": 5, "quantity": 5},
            {"delta": 0},
            {"quantity": -1},
            {"delta": 1.5},
            {"reason": "no mode given"},
        ],
    )
    async def test_malformed_bodies_are_422(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category, body
    ):
        product = await make_product(session, category, stock_quantity=8)
        slug = product.slug
        response = await client.patch(
            f"/api/v1/products/{slug}/stock", json=body, headers=admin_headers
        )
        assert response.status_code == 422, response.text
        stored = (await client.get(f"/api/v1/products/{slug}")).json()["data"]["product"]
        assert stored["stock_quantity"] == 8

    async def test_requires_authentication(self, client: AsyncClient, session: AsyncSession, category):
        product = await make_product(session, category, stock_quantity=5)
        response = await client.patch(
            f"/api/v1/products/{product.slug}/stock", json={"delta": 1}
        )
        assert response.status_code == 401

    async def test_customer_cannot_adjust_stock(
        self, client: AsyncClient, session: AsyncSession, category
    ):
        product = await make_product(session, category, stock_quantity=5)
        user = await _make_user(session)
        headers = await auth_header_for(client, user)
        response = await client.patch(
            f"/api/v1/products/{product.slug}/stock", json={"delta": 1}, headers=headers
        )
        assert response.status_code == 403
        assert response.json()["error_code"] == "insufficient_role"


class TestInventoryReport:
    """``GET /products/inventory`` surfaces real quantities for restocking."""

    async def _catalogue(self, session: AsyncSession, category):
        await make_product(session, category, name="Plenty", stock_quantity=500)
        await make_product(session, category, name="Scarce", stock_quantity=2)
        await make_product(session, category, name="Gone", stock_quantity=0)

    async def test_reports_quantities_scarcest_first(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        await self._catalogue(session, category)
        response = await client.get("/api/v1/products/inventory", headers=admin_headers)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert [p["name"] for p in data["products"]] == ["Gone", "Scarce", "Plenty"]
        assert [p["stock_quantity"] for p in data["products"]] == [0, 2, 500]

    async def test_labels_the_low_stock_band(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        await self._catalogue(session, category)
        data = (await client.get("/api/v1/products/inventory", headers=admin_headers)).json()["data"]
        flags = {p["name"]: p["is_low_stock"] for p in data["products"]}
        assert flags == {"Gone": False, "Scarce": True, "Plenty": False}

    async def test_tallies_describe_the_whole_catalogue(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        await self._catalogue(session, category)
        response = await client.get(
            "/api/v1/products/inventory", params={"limit": 1}, headers=admin_headers
        )
        data = response.json()["data"]
        assert len(data["products"]) == 1
        assert data["total"] == 3
        assert data["pages"] == 3
        assert (data["out_of_stock"], data["low_stock"], data["in_stock"]) == (1, 1, 2)

    async def test_low_stock_filter(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        await self._catalogue(session, category)
        response = await client.get(
            "/api/v1/products/inventory", params={"low_stock": True}, headers=admin_headers
        )
        data = response.json()["data"]
        assert [p["name"] for p in data["products"]] == ["Scarce"]
        assert data["total"] == 1

    async def test_out_of_stock_filter(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        await self._catalogue(session, category)
        response = await client.get(
            "/api/v1/products/inventory", params={"out_of_stock": True}, headers=admin_headers
        )
        assert [p["name"] for p in response.json()["data"]["products"]] == ["Gone"]

    async def test_threshold_is_configurable(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        await make_product(session, category, name="Ten", stock_quantity=10)
        await make_product(session, category, name="Eleven", stock_quantity=11)
        response = await client.get(
            "/api/v1/products/inventory",
            params={"low_stock_threshold": 10, "low_stock": True},
            headers=admin_headers,
        )
        assert [p["name"] for p in response.json()["data"]["products"]] == ["Ten"]

    async def test_custom_threshold_agrees_with_its_own_tally(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        """The header count must match the rows the same response labels.

        A tally pinned to the default threshold would report "1 low" next to
        three rows flagged low, which is worse than no count at all.
        """
        await make_product(session, category, name="Five", stock_quantity=5)
        await make_product(session, category, name="Twenty", stock_quantity=20)
        await make_product(session, category, name="Thirty", stock_quantity=30)
        response = await client.get(
            "/api/v1/products/inventory",
            params={"low_stock_threshold": 25},
            headers=admin_headers,
        )
        data = response.json()["data"]
        assert data["low_stock"] == 2
        assert sum(1 for p in data["products"] if p["is_low_stock"]) == data["low_stock"]
        # in_stock means "not out of stock"; low stock is a subset of it, not a
        # disjoint third bucket.
        assert data["in_stock"] + data["out_of_stock"] == data["total"]
        assert data["low_stock"] <= data["in_stock"]

    async def test_delisted_products_are_excluded_by_default(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        await make_product(session, category, name="Retired", stock_quantity=1, is_active=False)
        response = await client.get("/api/v1/products/inventory", headers=admin_headers)
        assert response.json()["data"]["total"] == 0

    async def test_delisted_products_can_be_included(
        self, client: AsyncClient, session: AsyncSession, admin_headers, category
    ):
        await make_product(session, category, name="Retired", stock_quantity=1, is_active=False)
        response = await client.get(
            "/api/v1/products/inventory", params={"include_inactive": True}, headers=admin_headers
        )
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["products"][0]["is_active"] is False

    async def test_requires_authentication(self, client: AsyncClient):
        assert (await client.get("/api/v1/products/inventory")).status_code == 401

    async def test_customer_cannot_read_inventory(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session)
        headers = await auth_header_for(client, user)
        response = await client.get("/api/v1/products/inventory", headers=headers)
        assert response.status_code == 403

    async def test_conflicting_filters_are_rejected(
        self, client: AsyncClient, admin_headers
    ):
        response = await client.get(
            "/api/v1/products/inventory",
            params={"low_stock": True, "out_of_stock": True},
            headers=admin_headers,
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "validation_error"

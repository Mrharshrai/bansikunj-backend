"""Category endpoint tests."""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _make_user, auth_header_for, make_category, make_product

pytestmark = pytest.mark.asyncio


def create_body(**overrides) -> dict:
    body = {"name": f"Cakes {uuid.uuid4().hex[:6]}", "description": "Fresh bakes"}
    body.update(overrides)
    return body


class TestPublicRead:
    async def test_list_is_public(self, client: AsyncClient, session: AsyncSession):
        await make_category(session, name="Cakes", slug="cakes")
        await make_category(session, name="Cookies", slug="cookies")
        response = await client.get("/api/v1/categories")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert {c["slug"] for c in data["categories"]} == {"cakes", "cookies"}

    async def test_inactive_categories_are_hidden(self, client: AsyncClient, session: AsyncSession):
        await make_category(session, name="Visible", slug="visible")
        await make_category(session, name="Hidden", slug="hidden", is_active=False)

        public = await client.get("/api/v1/categories")
        assert {c["slug"] for c in public.json()["data"]["categories"]} == {"visible"}

    async def test_include_inactive_is_ignored_for_anonymous(
        self, client: AsyncClient, session: AsyncSession
    ):
        await make_category(session, name="Hidden", slug="hidden", is_active=False)
        response = await client.get("/api/v1/categories", params={"include_inactive": True})
        assert response.status_code == 200
        assert response.json()["data"]["total"] == 0, (
            "a non-admin must not be able to reveal hidden categories"
        )

    async def test_include_inactive_works_for_an_admin(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        await make_category(session, name="Visible", slug="visible")
        await make_category(session, name="Hidden", slug="hidden", is_active=False)
        response = await client.get(
            "/api/v1/categories",
            params={"include_inactive": True},
            headers=admin_headers,
        )
        assert {c["slug"] for c in response.json()["data"]["categories"]} == {
            "visible",
            "hidden",
        }

    async def test_display_order_then_name(self, client: AsyncClient, session: AsyncSession):
        await make_category(session, name="Zebra", slug="zebra", display_order=1)
        await make_category(session, name="Apple", slug="apple", display_order=1)
        await make_category(session, name="First", slug="first", display_order=0)
        response = await client.get("/api/v1/categories")
        slugs = [c["slug"] for c in response.json()["data"]["categories"]]
        assert slugs == ["first", "apple", "zebra"]

    async def test_product_count_counts_only_active_products(
        self, client: AsyncClient, session: AsyncSession
    ):
        cat = await make_category(session, name="Cakes", slug="cakes")
        await make_product(session, cat)
        await make_product(session, cat)
        await make_product(session, cat, is_active=False)

        response = await client.get("/api/v1/categories/cakes")
        assert response.json()["data"]["category"]["product_count"] == 2

    async def test_get_by_slug(self, client: AsyncClient, session: AsyncSession):
        await make_category(session, name="Cookies", slug="soft-chewy", description="Chewy")
        response = await client.get("/api/v1/categories/soft-chewy")
        assert response.status_code == 200
        assert response.json()["data"]["category"]["name"] == "Cookies"

    async def test_unknown_slug_returns_404(self, client: AsyncClient):
        response = await client.get("/api/v1/categories/nope")
        assert response.status_code == 404
        assert response.json()["error_code"] == "category_not_found"

    async def test_inactive_category_slug_returns_404(self, client: AsyncClient, session: AsyncSession):
        await make_category(session, name="Gone", slug="gone", is_active=False)
        assert (await client.get("/api/v1/categories/gone")).status_code == 404


class TestCreate:
    async def test_admin_creates_a_category(self, client: AsyncClient, admin_headers):
        response = await client.post(
            "/api/v1/categories",
            json=create_body(name="Gift Boxes", description="Ready to gift"),
            headers=admin_headers,
        )
        assert response.status_code == 201, response.text
        data = response.json()["data"]["category"]
        assert data["name"] == "Gift Boxes"
        assert data["slug"] == "gift-boxes", "slug should be derived from the name"
        assert data["is_active"] is True

    async def test_slug_is_slugified(self, client: AsyncClient, admin_headers):
        response = await client.post(
            "/api/v1/categories",
            json=create_body(name="Crème Brûlée & Co!"),
            headers=admin_headers,
        )
        assert response.json()["data"]["category"]["slug"] == "creme-brulee-co"

    async def test_explicit_slug_is_kept(self, client: AsyncClient, admin_headers):
        response = await client.post(
            "/api/v1/categories",
            json=create_body(name="Cakes", slug="cakes"),
            headers=admin_headers,
        )
        assert response.json()["data"]["category"]["slug"] == "cakes"

    async def test_duplicate_slug_gets_a_suffix(self, client: AsyncClient, admin_headers):
        first = await client.post(
            "/api/v1/categories", json=create_body(name="Cakes", slug="cakes"), headers=admin_headers
        )
        assert first.json()["data"]["category"]["slug"] == "cakes"
        second = await client.post(
            "/api/v1/categories", json=create_body(name="Cakes Too", slug="cakes"), headers=admin_headers
        )
        assert second.status_code == 201
        assert second.json()["data"]["category"]["slug"] == "cakes-2"

    async def test_duplicate_name_returns_409(self, client: AsyncClient, admin_headers):
        await client.post("/api/v1/categories", json=create_body(name="Cakes"), headers=admin_headers)
        second = await client.post(
            "/api/v1/categories", json=create_body(name="cakes"), headers=admin_headers
        )
        assert second.status_code == 409
        assert second.json()["error_code"] == "category_name_taken"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"name": ""},
            {"name": "  "},
            {"image_url": "not-a-url"},
            {"display_order": -1},
        ],
    )
    async def test_invalid_payloads_return_422(
        self, client: AsyncClient, admin_headers, overrides: dict
    ):
        response = await client.post(
            "/api/v1/categories", json=create_body(**overrides), headers=admin_headers
        )
        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == "validation_error"

    async def test_bad_slug_returns_422(self, client: AsyncClient, admin_headers):
        response = await client.post(
            "/api/v1/categories", json=create_body(slug="Not A Slug"), headers=admin_headers
        )
        assert response.status_code == 422

    async def test_requires_authentication(self, client: AsyncClient):
        assert (await client.post("/api/v1/categories", json=create_body())).status_code == 401

    async def test_customer_is_forbidden(self, client: AsyncClient, session: AsyncSession):
        user = await _make_user(session)
        headers = await auth_header_for(client, user)
        response = await client.post("/api/v1/categories", json=create_body(), headers=headers)
        assert response.status_code == 403
        assert response.json()["error_code"] == "insufficient_role"


class TestUpdate:
    async def _create(self, client, headers) -> dict:
        response = await client.post(
            "/api/v1/categories", json=create_body(name="Cakes"), headers=headers
        )
        return response.json()["data"]["category"]

    async def test_patch_updates_only_given_fields(
        self, client: AsyncClient, admin_headers
    ):
        created = await self._create(client, admin_headers)
        response = await client.patch(
            f"/api/v1/categories/{created['id']}",
            json={"description": "Updated"},
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()["data"]["category"]
        assert data["description"] == "Updated"
        assert data["name"] == created["name"], "untouched fields must survive"
        assert data["slug"] == created["slug"]

    async def test_put_and_patch_agree(
        self, client: AsyncClient, admin_headers
    ):
        created = await self._create(client, admin_headers)
        put = await client.put(
            f"/api/v1/categories/{created['id']}",
            json={"name": "Cakes Renamed", "description": "d", "display_order": 5},
            headers=admin_headers,
        )
        assert put.status_code == 200
        assert put.json()["data"]["category"]["name"] == "Cakes Renamed"

    async def test_changing_name_keeps_slug(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        cat = await make_category(session, name="Cakes", slug="cakes")
        response = await client.patch(
            f"/api/v1/categories/{cat.id}", json={"name": "Patisserie"}, headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["data"]["category"]["slug"] == "cakes", (
            "renaming must not break existing product links"
        )

    async def test_explicit_slug_change_is_applied(
        self, client: AsyncClient, admin_headers
    ):
        created = await self._create(client, admin_headers)
        response = await client.patch(
            f"/api/v1/categories/{created['id']}", json={"slug": "bakes"}, headers=admin_headers
        )
        assert response.json()["data"]["category"]["slug"] == "bakes"

    async def test_conflicting_name_returns_409(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        await make_category(session, name="Taken", slug="taken")
        cat = await make_category(session, name="Free", slug="free")
        response = await client.patch(
            f"/api/v1/categories/{cat.id}", json={"name": "Taken"}, headers=admin_headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "category_name_taken"

    async def test_unknown_category_returns_404(self, client: AsyncClient, admin_headers):
        # A valid name: the point here is the missing category, not the payload.
        # A too-short name would be refused by min_length with a 422 and the
        # endpoint would never run.
        response = await client.patch(
            f"/api/v1/categories/{uuid.uuid4()}",
            json={"name": "Ghost"},
            headers=admin_headers,
        )
        assert response.status_code == 404

    async def test_customer_is_forbidden(self, client: AsyncClient, session: AsyncSession):
        cat = await make_category(session)
        user = await _make_user(session)
        headers = await auth_header_for(client, user)
        response = await client.patch(
            f"/api/v1/categories/{cat.id}", json={"name": "Hijack"}, headers=headers
        )
        assert response.status_code == 403


class TestDeactivateAndDelete:
    async def test_deactivate_hides_the_category(self, client: AsyncClient, session: AsyncSession, admin_headers):
        cat = await make_category(session, name="Cakes", slug="cakes")
        response = await client.post(
            f"/api/v1/categories/{cat.id}/deactivate", headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["data"]["category"]["is_active"] is False
        assert (await client.get("/api/v1/categories/cakes")).status_code == 404

    async def test_deactivate_refuses_while_products_exist(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        cat = await make_category(session, name="Cakes", slug="cakes")
        await make_product(session, cat)
        response = await client.post(
            f"/api/v1/categories/{cat.id}/deactivate", headers=admin_headers
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == "category_not_empty"
        assert response.json()["details"]["product_count"] == 1

    async def test_deactivate_with_force_succeeds(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        cat = await make_category(session, name="Cakes", slug="cakes")
        await make_product(session, cat)
        response = await client.post(
            f"/api/v1/categories/{cat.id}/deactivate",
            params={"force": True},
            headers=admin_headers,
        )
        assert response.status_code == 200

    async def test_delete_empty_category(self, client: AsyncClient, session: AsyncSession, admin_headers):
        cat = await make_category(session, name="Cakes", slug="cakes")
        response = await client.delete(f"/api/v1/categories/{cat.id}", headers=admin_headers)
        assert response.status_code == 200
        assert (await client.get("/api/v1/categories/cakes")).status_code == 404

    async def test_delete_refuses_while_products_exist(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        cat = await make_category(session, name="Cakes", slug="cakes")
        await make_product(session, cat)
        response = await client.delete(f"/api/v1/categories/{cat.id}", headers=admin_headers)
        assert response.status_code == 409
        assert response.json()["error_code"] == "category_not_empty"

    async def test_customer_cannot_delete(self, client: AsyncClient, session: AsyncSession):
        cat = await make_category(session)
        user = await _make_user(session)
        headers = await auth_header_for(client, user)
        assert (
            await client.delete(f"/api/v1/categories/{cat.id}", headers=headers)
        ).status_code == 403

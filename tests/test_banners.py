"""Banner endpoint tests."""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import make_banner

pytestmark = pytest.mark.asyncio

BANNERS = "/api/v1/banners"


def create_body(**overrides) -> dict:
    body = {
        "title": "Festive Sale",
        "subtitle": "Up to 40% off",
        "image_url": "/assets/banners/festive.png",
        "link_url": "/products?tag=festive",
    }
    body.update(overrides)
    return body


class TestPublicRead:
    async def test_list_is_public(self, client: AsyncClient, session: AsyncSession):
        await make_banner(session, title="One", display_order=0)
        await make_banner(session, title="Two", display_order=1)
        response = await client.get(BANNERS)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2
        assert [b["title"] for b in data["banners"]] == ["One", "Two"]

    async def test_inactive_banners_are_hidden(
        self, client: AsyncClient, session: AsyncSession
    ):
        await make_banner(session, title="Visible")
        await make_banner(session, title="Hidden", is_active=False)

        public = await client.get(BANNERS)
        assert [b["title"] for b in public.json()["data"]["banners"]] == ["Visible"]

    async def test_include_inactive_is_ignored_for_anonymous(
        self, client: AsyncClient, session: AsyncSession
    ):
        await make_banner(session, title="Hidden", is_active=False)
        response = await client.get(BANNERS, params={"include_inactive": True})
        assert response.status_code == 200
        assert response.json()["data"]["total"] == 0, (
            "a non-admin must not be able to reveal hidden banners"
        )

    async def test_include_inactive_is_ignored_for_a_customer(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        await make_banner(session, title="Hidden", is_active=False)
        response = await client.get(
            BANNERS, params={"include_inactive": True}, headers=customer_headers
        )
        assert response.json()["data"]["total"] == 0

    async def test_include_inactive_works_for_an_admin(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        await make_banner(session, title="Visible")
        await make_banner(session, title="Hidden", is_active=False)
        response = await client.get(
            BANNERS, params={"include_inactive": True}, headers=admin_headers
        )
        assert {b["title"] for b in response.json()["data"]["banners"]} == {
            "Visible",
            "Hidden",
        }

    async def test_display_order_then_title(
        self, client: AsyncClient, session: AsyncSession
    ):
        await make_banner(session, title="Zebra", display_order=1)
        await make_banner(session, title="Apple", display_order=1)
        await make_banner(session, title="First", display_order=0)
        response = await client.get(BANNERS)
        titles = [b["title"] for b in response.json()["data"]["banners"]]
        assert titles == ["First", "Apple", "Zebra"]

    async def test_banner_shape(self, client: AsyncClient, session: AsyncSession):
        banner = await make_banner(
            session,
            title="Festive Sale",
            subtitle="Up to 40% off",
            image_url="/assets/banners/festive.png",
            link_url="/products?tag=festive",
            display_order=3,
        )
        response = await client.get(BANNERS)
        row = response.json()["data"]["banners"][0]
        assert row["id"] == str(banner.id)
        assert row["title"] == "Festive Sale"
        assert row["subtitle"] == "Up to 40% off"
        assert row["image_url"] == "/assets/banners/festive.png"
        assert row["link_url"] == "/products?tag=festive"
        assert row["is_active"] is True
        assert row["display_order"] == 3
        assert row["created_at"] and row["updated_at"]

    async def test_get_by_id(self, client: AsyncClient, session: AsyncSession):
        banner = await make_banner(session, title="Festive Sale")
        response = await client.get(f"{BANNERS}/{banner.id}")
        assert response.status_code == 200
        assert response.json()["data"]["banner"]["title"] == "Festive Sale"

    async def test_unknown_id_returns_404(self, client: AsyncClient):
        response = await client.get(f"{BANNERS}/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["error_code"] == "banner_not_found"

    async def test_malformed_id_returns_422(self, client: AsyncClient):
        response = await client.get(f"{BANNERS}/not-a-uuid")
        assert response.status_code == 422
        assert response.json()["error_code"] == "validation_error"

    async def test_inactive_banner_is_404_for_the_public(
        self, client: AsyncClient, session: AsyncSession
    ):
        banner = await make_banner(session, title="Hidden", is_active=False)
        assert (await client.get(f"{BANNERS}/{banner.id}")).status_code == 404

    async def test_inactive_banner_is_visible_to_an_admin(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Hidden", is_active=False)
        response = await client.get(f"{BANNERS}/{banner.id}", headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["data"]["banner"]["title"] == "Hidden"


class TestCreate:
    async def test_admin_creates_a_banner(self, client: AsyncClient, admin_headers):
        response = await client.post(
            BANNERS, json=create_body(), headers=admin_headers
        )
        assert response.status_code == 201, response.text
        banner = response.json()["data"]["banner"]
        assert banner["title"] == "Festive Sale"
        assert banner["image_url"] == "/assets/banners/festive.png"
        assert banner["is_active"] is True
        assert banner["display_order"] == 0
        assert banner["subtitle"] == "Up to 40% off"
        assert banner["link_url"] == "/products?tag=festive"

    async def test_created_banner_appears_in_the_public_list(
        self, client: AsyncClient, admin_headers
    ):
        created = await client.post(
            BANNERS, json=create_body(title="Announcement"), headers=admin_headers
        )
        banner_id = created.json()["data"]["banner"]["id"]

        public = await client.get(BANNERS)
        assert [b["id"] for b in public.json()["data"]["banners"]] == [banner_id]

    async def test_optional_fields_default_to_null(
        self, client: AsyncClient, admin_headers
    ):
        response = await client.post(
            BANNERS,
            json={"title": "Plain", "image_url": "/assets/banners/plain.png"},
            headers=admin_headers,
        )
        banner = response.json()["data"]["banner"]
        assert banner["subtitle"] is None
        assert banner["link_url"] is None

    async def test_anonymous_create_returns_401(self, client: AsyncClient):
        response = await client.post(BANNERS, json=create_body())
        assert response.status_code == 401
        assert response.json()["error_code"] == "missing_credentials"

    async def test_customer_create_returns_403(
        self, client: AsyncClient, customer_headers
    ):
        response = await client.post(
            BANNERS, json=create_body(), headers=customer_headers
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error_code"] == "insufficient_role"
        assert body["details"] == {"required_roles": ["admin"]}

    @pytest.mark.parametrize(
        "overrides",
        [
            {"title": ""},
            {"title": "   "},
            {"title": None},
            {"image_url": None},
            {"image_url": ""},
            {"image_url": "ftp://example.com/banner.png"},
            {"image_url": "example.com/banner.png"},
            {"link_url": "javascript:alert(1)"},
            {"subtitle": "   "},
            {"title": "x" * 151},
            {"display_order": -1},
            {"image_url": "/assets/" + "a" * 500},
        ],
        ids=lambda o: str(sorted(o.items())),
    )
    async def test_invalid_payloads_return_422(
        self, client: AsyncClient, admin_headers, overrides: dict
    ):
        response = await client.post(
            BANNERS, json=create_body(**overrides), headers=admin_headers
        )
        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == "validation_error"


class TestUpdate:
    async def test_patch_changes_only_the_supplied_fields(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(
            session, title="Original", subtitle="Original sub", display_order=2
        )
        response = await client.patch(
            f"{BANNERS}/{banner.id}",
            json={"title": "Renamed"},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        updated = response.json()["data"]["banner"]
        assert updated["title"] == "Renamed"
        assert updated["subtitle"] == "Original sub"
        assert updated["display_order"] == 2

    async def test_put_updates_multiple_fields(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Original")
        response = await client.put(
            f"{BANNERS}/{banner.id}",
            json={
                "title": "Replaced",
                "subtitle": "New sub",
                "image_url": "/assets/banners/new.png",
                "link_url": "/cart",
                "is_active": False,
                "display_order": 7,
            },
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        updated = response.json()["data"]["banner"]
        assert updated["title"] == "Replaced"
        assert updated["subtitle"] == "New sub"
        assert updated["image_url"] == "/assets/banners/new.png"
        assert updated["link_url"] == "/cart"
        assert updated["is_active"] is False
        assert updated["display_order"] == 7

    async def test_deactivating_hides_it_from_the_public_list(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Festive")
        response = await client.patch(
            f"{BANNERS}/{banner.id}", json={"is_active": False}, headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["data"]["banner"]["is_active"] is False

        public = await client.get(BANNERS)
        assert public.json()["data"]["total"] == 0
        assert (await client.get(f"{BANNERS}/{banner.id}")).status_code == 404

    async def test_subtitle_can_be_cleared(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Festive", subtitle="Old sub")
        response = await client.patch(
            f"{BANNERS}/{banner.id}", json={"subtitle": None}, headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["data"]["banner"]["subtitle"] is None

    async def test_link_can_be_cleared(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Festive", link_url="/products")
        response = await client.patch(
            f"{BANNERS}/{banner.id}", json={"link_url": None}, headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["data"]["banner"]["link_url"] is None

    async def test_image_cannot_be_cleared(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Festive")
        response = await client.patch(
            f"{BANNERS}/{banner.id}", json={"image_url": None}, headers=admin_headers
        )
        assert response.status_code == 422, response.text

    async def test_title_cannot_be_cleared(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Festive")
        response = await client.patch(
            f"{BANNERS}/{banner.id}", json={"title": None}, headers=admin_headers
        )
        assert response.status_code == 422

    async def test_an_admin_can_update_a_hidden_banner(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Hidden", is_active=False)
        response = await client.patch(
            f"{BANNERS}/{banner.id}", json={"title": "Revived"}, headers=admin_headers
        )
        assert response.status_code == 200
        assert response.json()["data"]["banner"]["title"] == "Revived"

    async def test_unknown_id_returns_404(
        self, client: AsyncClient, admin_headers
    ):
        response = await client.patch(
            f"{BANNERS}/{uuid.uuid4()}", json={"title": "Nope"}, headers=admin_headers
        )
        assert response.status_code == 404
        assert response.json()["error_code"] == "banner_not_found"

    async def test_anonymous_update_returns_401(
        self, client: AsyncClient, session: AsyncSession
    ):
        banner = await make_banner(session, title="Festive")
        response = await client.patch(
            f"{BANNERS}/{banner.id}", json={"title": "Hijacked"}
        )
        assert response.status_code == 401

    async def test_customer_update_returns_403(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        banner = await make_banner(session, title="Festive")
        response = await client.patch(
            f"{BANNERS}/{banner.id}",
            json={"title": "Nope"},
            headers=customer_headers,
        )
        assert response.status_code == 403
        assert response.json()["error_code"] == "insufficient_role"


class TestDelete:
    async def test_admin_deletes_a_banner(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Festive")
        response = await client.delete(f"{BANNERS}/{banner.id}", headers=admin_headers)
        assert response.status_code == 200, response.text
        assert response.json()["message"] == "Banner deleted successfully"

        assert (await client.get(f"{BANNERS}/{banner.id}")).status_code == 404
        assert (await client.get(BANNERS)).json()["data"]["total"] == 0

    async def test_admin_can_delete_a_hidden_banner(
        self, client: AsyncClient, session: AsyncSession, admin_headers
    ):
        banner = await make_banner(session, title="Hidden", is_active=False)
        response = await client.delete(f"{BANNERS}/{banner.id}", headers=admin_headers)
        assert response.status_code == 200

    async def test_unknown_id_returns_404(
        self, client: AsyncClient, admin_headers
    ):
        response = await client.delete(f"{BANNERS}/{uuid.uuid4()}", headers=admin_headers)
        assert response.status_code == 404
        assert response.json()["error_code"] == "banner_not_found"

    async def test_anonymous_delete_returns_401(
        self, client: AsyncClient, session: AsyncSession
    ):
        banner = await make_banner(session, title="Festive")
        assert (await client.delete(f"{BANNERS}/{banner.id}")).status_code == 401

    async def test_customer_delete_returns_403(
        self, client: AsyncClient, session: AsyncSession, customer_headers
    ):
        banner = await make_banner(session, title="Festive")
        response = await client.delete(
            f"{BANNERS}/{banner.id}", headers=customer_headers
        )
        assert response.status_code == 403
        assert response.json()["error_code"] == "insufficient_role"

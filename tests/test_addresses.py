"""Address tests, with emphasis on cross-user isolation."""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _make_user, auth_header

pytestmark = pytest.mark.asyncio

VALID_ADDRESS = {
    "label": "Home",
    "full_name": "Asha Rao",
    "phone": "9876543210",
    "line1": "12 MG Road",
    "line2": "Near Railway Station",
    "city": "Pune",
    "state": "Maharashtra",
    "pincode": "411001",
    "country": "IN",
}


async def _create(client: AsyncClient, headers: dict, **overrides) -> dict:
    payload = {**VALID_ADDRESS, **overrides}
    response = await client.post("/api/v1/addresses", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["data"]["address"]


class TestCreate:
    async def test_create_returns_201(self, client: AsyncClient, customer):
        headers = await auth_header(client, email=customer.email)
        address = await _create(client, headers)
        assert address["user_id"] == str(customer.id)
        assert address["city"] == "Pune"
        assert address["is_default"] is True  # first address becomes default

    async def test_first_address_is_automatically_default(
        self, client: AsyncClient, customer
    ):
        headers = await auth_header(client, email=customer.email)
        assert (await _create(client, headers))["is_default"] is True

    async def test_second_address_is_not_default_by_default(
        self, client: AsyncClient, customer
    ):
        headers = await auth_header(client, email=customer.email)
        await _create(client, headers)
        second = await _create(client, headers, label="Work")
        assert second["is_default"] is False

    async def test_second_address_can_request_default(
        self, client: AsyncClient, customer
    ):
        headers = await auth_header(client, email=customer.email)
        await _create(client, headers)
        second = await _create(client, headers, is_default=True)
        assert second["is_default"] is True

        listing = await client.get("/api/v1/addresses", headers=headers)
        defaults = [a for a in listing.json()["data"]["addresses"] if a["is_default"]]
        assert len(defaults) == 1, "only one address may be default"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"phone": "123"},
            {"pincode": "1"},
            {"city": "  "},
            {"line1": ""},
            {"country": "IND"},
        ],
    )
    async def test_invalid_addresses_return_422(
        self, client: AsyncClient, customer, overrides: dict
    ):
        headers = await auth_header(client, email=customer.email)
        response = await client.post(
            "/api/v1/addresses", json={**VALID_ADDRESS, **overrides}, headers=headers
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "validation_error"

    async def test_create_requires_authentication(self, client: AsyncClient):
        response = await client.post("/api/v1/addresses", json=VALID_ADDRESS)
        assert response.status_code == 401


class TestRead:
    async def test_list_returns_only_own_addresses(
        self, client: AsyncClient, session: AsyncSession, other_customer
    ):
        mine = await _make_user(session, email=f"mine-{uuid.uuid4().hex[:8]}@example.com")
        headers = await auth_header(client, email=mine.email)
        await _create(client, headers, label="Mine")

        other_headers = await auth_header(client, email=other_customer.email)
        await _create(client, other_headers, label="Theirs")

        response = await client.get("/api/v1/addresses", headers=headers)
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 1
        assert data["addresses"][0]["label"] == "Mine"

    async def test_get_own_address(self, client: AsyncClient, customer):
        headers = await auth_header(client, email=customer.email)
        created = await _create(client, headers)
        response = await client.get(f"/api/v1/addresses/{created['id']}", headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["address"]["id"] == created["id"]

    async def test_get_another_users_address_returns_404(
        self, client: AsyncClient, session: AsyncSession, other_customer
    ):
        """Cross-tenant read must be indistinguishable from a missing resource."""
        victim = await _make_user(session, email=f"vic-{uuid.uuid4().hex[:8]}@example.com")
        victim_headers = await auth_header(client, email=victim.email)
        secret = await _create(client, victim_headers, label="Victim Home")

        attacker = await _make_user(session, email=f"atk-{uuid.uuid4().hex[:8]}@example.com")
        attacker_headers = await auth_header(client, email=attacker.email)

        stolen = await client.get(
            f"/api/v1/addresses/{secret['id']}", headers=attacker_headers
        )
        missing = await client.get(
            f"/api/v1/addresses/{uuid.uuid4()}", headers=attacker_headers
        )
        assert stolen.status_code == missing.status_code == 404
        assert stolen.json() == missing.json()
        assert "Victim Home" not in stolen.text


class TestUpdate:
    async def test_patch_updates_only_provided_fields(
        self, client: AsyncClient, customer
    ):
        headers = await auth_header(client, email=customer.email)
        created = await _create(client, headers)

        response = await client.patch(
            f"/api/v1/addresses/{created['id']}", json={"city": "Mumbai"}, headers=headers
        )
        assert response.status_code == 200
        data = response.json()["data"]["address"]
        assert data["city"] == "Mumbai"
        assert data["line1"] == VALID_ADDRESS["line1"]  # untouched
        assert data["state"] == VALID_ADDRESS["state"]

    async def test_cannot_patch_another_users_address(
        self, client: AsyncClient, session: AsyncSession, other_customer
    ):
        victim = await _make_user(session, email=f"vic2-{uuid.uuid4().hex[:8]}@example.com")
        victim_headers = await auth_header(client, email=victim.email)
        secret = await _create(client, victim_headers)

        attacker = await _make_user(session, email=f"atk2-{uuid.uuid4().hex[:8]}@example.com")
        attacker_headers = await auth_header(client, email=attacker.email)
        response = await client.patch(
            f"/api/v1/addresses/{secret['id']}",
            json={"city": "Hijacked"},
            headers=attacker_headers,
        )
        assert response.status_code == 404

        check = await client.get(
            f"/api/v1/addresses/{secret['id']}", headers=victim_headers
        )
        assert check.json()["data"]["address"]["city"] == "Pune"


class TestSetDefault:
    async def test_set_default_moves_the_flag(
        self, client: AsyncClient, customer
    ):
        headers = await auth_header(client, email=customer.email)
        first = await _create(client, headers, label="Home")
        second = await _create(client, headers, label="Work")
        assert first["is_default"] is True

        response = await client.post(
            f"/api/v1/addresses/{second['id']}/default", headers=headers
        )
        assert response.status_code == 200
        assert response.json()["data"]["address"]["is_default"] is True

        listing = await client.get("/api/v1/addresses", headers=headers)
        defaults = [a for a in listing.json()["data"]["addresses"] if a["is_default"]]
        assert len(defaults) == 1
        assert defaults[0]["id"] == second["id"]

    async def test_cannot_set_another_users_address_as_default(
        self, client: AsyncClient, session: AsyncSession, other_customer
    ):
        victim = await _make_user(session, email=f"vic3-{uuid.uuid4().hex[:8]}@example.com")
        victim_headers = await auth_header(client, email=victim.email)
        secret = await _create(client, victim_headers)

        attacker = await _make_user(session, email=f"atk3-{uuid.uuid4().hex[:8]}@example.com")
        attacker_headers = await auth_header(client, email=attacker.email)
        response = await client.post(
            f"/api/v1/addresses/{secret['id']}/default", headers=attacker_headers
        )
        assert response.status_code == 404


class TestDelete:
    async def test_delete_own_address(self, client: AsyncClient, customer):
        headers = await auth_header(client, email=customer.email)
        created = await _create(client, headers)
        response = await client.delete(f"/api/v1/addresses/{created['id']}", headers=headers)
        assert response.status_code == 200
        listing = await client.get("/api/v1/addresses", headers=headers)
        assert listing.json()["data"]["total"] == 0

    async def test_deleting_default_promotes_another(self, client: AsyncClient, customer):
        headers = await auth_header(client, email=customer.email)
        first = await _create(client, headers, label="Home")
        second = await _create(client, headers, label="Work")

        await client.delete(f"/api/v1/addresses/{first['id']}", headers=headers)

        listing = await client.get("/api/v1/addresses", headers=headers)
        remaining = listing.json()["data"]["addresses"]
        assert len(remaining) == 1
        assert remaining[0]["id"] == second["id"]
        assert remaining[0]["is_default"] is True

    async def test_cannot_delete_another_users_address(
        self, client: AsyncClient, session: AsyncSession, other_customer
    ):
        victim = await _make_user(session, email=f"vic4-{uuid.uuid4().hex[:8]}@example.com")
        victim_headers = await auth_header(client, email=victim.email)
        secret = await _create(client, victim_headers)

        attacker = await _make_user(session, email=f"atk4-{uuid.uuid4().hex[:8]}@example.com")
        attacker_headers = await auth_header(client, email=attacker.email)
        response = await client.delete(
            f"/api/v1/addresses/{secret['id']}", headers=attacker_headers
        )
        assert response.status_code == 404

        check = await client.get("/api/v1/addresses", headers=victim_headers)
        assert check.json()["data"]["total"] == 1

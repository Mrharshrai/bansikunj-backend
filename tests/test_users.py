"""User profile endpoint tests."""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import _make_user, auth_header

pytestmark = pytest.mark.asyncio


async def test_get_me_returns_the_caller(client: AsyncClient, customer):
    headers = await auth_header(client, email=customer.email)
    response = await client.get("/api/v1/users/me", headers=headers)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == str(customer.id)
    assert data["email"] == customer.email
    assert data["role"] == "customer"
    assert data["is_active"] is True


async def test_get_me_never_returns_the_password_hash(client: AsyncClient, customer):
    headers = await auth_header(client, email=customer.email)
    response = await client.get("/api/v1/users/me", headers=headers)
    text = response.text
    assert "hashed_password" not in text
    assert "$argon2" not in text
    assert "password" not in response.json()["data"]


async def test_patch_me_updates_only_provided_fields(
    client: AsyncClient, session: AsyncSession
):
    user = await _make_user(
        session,
        email=f"patch-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Original Name",
    )
    headers = await auth_header(client, email=user.email)

    response = await client.patch(
        "/api/v1/users/me", json={"full_name": "Patched Name"}, headers=headers
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["full_name"] == "Patched Name"
    # phone was not sent, so it must survive untouched
    assert data["phone"] == user.phone


async def test_patch_me_with_empty_body_is_a_no_op(
    client: AsyncClient, session: AsyncSession
):
    user = await _make_user(session, email=f"noop-{uuid.uuid4().hex[:8]}@example.com")
    headers = await auth_header(client, email=user.email)
    response = await client.patch("/api/v1/users/me", json={}, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["full_name"] == user.full_name


async def test_put_me_updates_fields(client: AsyncClient, session: AsyncSession):
    user = await _make_user(session, email=f"put-{uuid.uuid4().hex[:8]}@example.com")
    headers = await auth_header(client, email=user.email)
    response = await client.put(
        "/api/v1/users/me",
        json={"full_name": "Replaced", "phone": "9123456789"},
        headers=headers,
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["full_name"] == "Replaced"
    assert data["phone"] == "9123456789"


@pytest.mark.parametrize(
    "payload",
    [
        {"full_name": "   "},
        {"full_name": ""},
        {"phone": "123"},
        {"phone": "abcdefghij"},
        {"phone": "1234567890123456789"},
    ],
)
async def test_invalid_profile_updates_return_422(
    client: AsyncClient, session: AsyncSession, payload: dict
):
    user = await _make_user(session, email=f"bad-{uuid.uuid4().hex[:8]}@example.com")
    headers = await auth_header(client, email=user.email)
    response = await client.patch("/api/v1/users/me", json=payload, headers=headers)
    assert response.status_code == 422
    assert response.json()["error_code"] == "validation_error"


async def test_profile_endpoints_require_authentication(client: AsyncClient):
    for method, path in (
        ("get", "/api/v1/users/me"),
        ("patch", "/api/v1/users/me"),
        ("put", "/api/v1/users/me"),
        ("post", "/api/v1/users/me/change-password"),
    ):
        # client.request(), not the per-method helper: HTTPX only accepts a
        # ``json`` body on POST/PUT/PATCH, so a bare ``client.get(json=...)``
        # raises TypeError instead of reaching the app.
        response = await client.request(method.upper(), path, json={})
        assert response.status_code == 401, f"{method.upper()} {path} should be 401"


async def test_role_cannot_be_changed_through_the_profile_endpoint(
    client: AsyncClient, session: AsyncSession
):
    """Privilege escalation attempt: extra keys in the body are ignored."""
    user = await _make_user(session, email=f"esc-{uuid.uuid4().hex[:8]}@example.com")
    headers = await auth_header(client, email=user.email)
    response = await client.patch(
        "/api/v1/users/me",
        json={"full_name": "Escalated", "role": "admin", "is_active": True},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["data"]["role"] == "customer"

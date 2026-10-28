"""Role-based access control via the ``require_role`` dependency.

Phase 2 ships no admin-only route, so these tests mount a throwaway one on the
app, exercise it, then remove it.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import APIRouter, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import require_role
from app.core.database import get_db
from app.main import app
from app.models.user import Role, User
from tests.conftest import _make_user, auth_header_for

pytestmark = pytest.mark.asyncio

PROBE = "/__test__/admin-only"


@pytest.fixture
def admin_probe():
    """Mount a temporary admin-only route and tear it down afterwards."""
    router = APIRouter()
    current_admin = require_role(Role.ADMIN)
    staff_or_admin = require_role(Role.STAFF, Role.ADMIN)

    @router.get("/__test__/admin-only")
    async def _admin_only(user: User = Depends(current_admin)):
        return {"ok": True, "user_id": str(user.id), "role": user.role.value}

    @router.get("/__test__/staff-or-admin")
    async def _staff_or_admin(user: User = Depends(staff_or_admin)):
        return {"ok": True, "role": user.role.value}

    app.include_router(router)
    yield
    app.router.routes = [r for r in app.router.routes if getattr(r, "path", None) not in
                         (PROBE, "/__test__/staff-or-admin")]
    app.openapi_schema = None


@pytest.fixture
async def probe_client(session: AsyncSession) -> AsyncClient:
    async def _override_get_db():
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_admin_can_reach_admin_route(
    admin_probe, probe_client, session: AsyncSession
):
    admin = await _make_user(session, role=Role.ADMIN, full_name="Boss")
    headers = await auth_header_for(probe_client, admin)
    response = await probe_client.get(PROBE, headers=headers)
    assert response.status_code == 200
    assert response.json()["role"] == "admin"


async def test_customer_is_forbidden_from_admin_route(
    admin_probe, probe_client, session: AsyncSession
):
    user = await _make_user(session, role=Role.CUSTOMER)
    headers = await auth_header_for(probe_client, user)
    response = await probe_client.get(PROBE, headers=headers)
    assert response.status_code == 403
    body = response.json()
    assert body["success"] is False
    assert body["error_code"] == "insufficient_role"
    assert body["details"] == {"required_roles": ["admin"]}


async def test_delivery_agent_cannot_reach_admin_route(
    admin_probe, probe_client, session: AsyncSession
):
    user = await _make_user(session, role=Role.DELIVERY_AGENT)
    headers = await auth_header_for(probe_client, user)
    assert (await probe_client.get(PROBE, headers=headers)).status_code == 403


async def test_multiple_allowed_roles_are_accepted(
    admin_probe, probe_client, session: AsyncSession
):
    for role in (Role.STAFF, Role.ADMIN):
        user = await _make_user(
            session, role=role, full_name=f"{role.value} user"
        )
        headers = await auth_header_for(probe_client, user)
        response = await probe_client.get("/__test__/staff-or-admin", headers=headers)
        assert response.status_code == 200, f"{role.value} should be allowed"

    customer = await _make_user(session, role=Role.CUSTOMER, full_name="Nope")
    headers = await auth_header_for(probe_client, customer)
    assert (
        await probe_client.get("/__test__/staff-or-admin", headers=headers)
    ).status_code == 403


async def test_role_route_requires_authentication(
    admin_probe, probe_client
):
    assert (await probe_client.get(PROBE)).status_code == 401


async def test_role_comes_from_the_database_not_the_token(
    admin_probe, probe_client, session: AsyncSession
):
    """A token minted while admin must lose access once demoted."""
    admin = await _make_user(session, role=Role.ADMIN, full_name="Demoted")
    headers = await auth_header_for(probe_client, admin)
    assert (await probe_client.get(PROBE, headers=headers)).status_code == 200

    repo_stored = await session.get(type(admin), admin.id)
    repo_stored.role = Role.CUSTOMER
    await session.commit()

    response = await probe_client.get(PROBE, headers=headers)
    assert response.status_code == 403, "role must be re-read from the database"


async def test_inactive_admin_is_blocked_even_with_a_valid_token(
    admin_probe, probe_client, session: AsyncSession
):
    admin = await _make_user(session, role=Role.ADMIN, full_name="Disabled")
    headers = await auth_header_for(probe_client, admin)
    assert (await probe_client.get(PROBE, headers=headers)).status_code == 200

    stored = await session.get(type(admin), admin.id)
    stored.is_active = False
    await session.commit()

    response = await probe_client.get(PROBE, headers=headers)
    assert response.status_code == 403
    assert response.json()["error_code"] == "account_inactive"

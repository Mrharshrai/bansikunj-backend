"""Authentication tests: register, login, refresh, logout, password flows."""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user_repository import UserRepository
from tests.conftest import VALID_PASSWORD, _make_user, auth_header, register_via_api

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------- register
class TestRegister:
    async def test_register_returns_201_with_tokens(self, client: AsyncClient):
        email = f"reg-{uuid.uuid4().hex[:8]}@example.com"
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": VALID_PASSWORD, "full_name": "Asha"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["success"] is True
        assert body["message"]
        data = body["data"]
        assert data["user"]["email"] == email
        assert data["user"]["full_name"] == "Asha"
        assert data["user"]["role"] == "customer"
        assert data["tokens"]["token_type"] == "bearer"
        assert data["tokens"]["access_token"]
        assert data["tokens"]["refresh_token"]
        assert data["tokens"]["expires_in"] > 0

    async def test_registration_never_exposes_password_hash(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"leak-{uuid.uuid4().hex[:8]}@example.com",
                "password": VALID_PASSWORD,
                "full_name": "Leaky",
            },
        )
        text = response.text
        assert "hashed_password" not in text
        assert "password_hash" not in text
        assert VALID_PASSWORD not in text
        assert "$argon2" not in text

    async def test_duplicate_email_returns_409(self, client: AsyncClient):
        email = f"dup-{uuid.uuid4().hex[:8]}@example.com"
        payload = {"email": email, "password": VALID_PASSWORD, "full_name": "Dup"}
        first = await client.post("/api/v1/auth/register", json=payload)
        assert first.status_code == 201
        second = await client.post("/api/v1/auth/register", json=payload)
        assert second.status_code == 409
        assert second.json()["error_code"] == "email_already_registered"

    async def test_email_is_normalised_to_lowercase(self, client: AsyncClient):
        local = uuid.uuid4().hex[:8]
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"Mixed.{local}@Example.COM",
                "password": VALID_PASSWORD,
                "full_name": "Casey",
            },
        )
        assert response.status_code == 201
        assert response.json()["data"]["user"]["email"] == f"mixed.{local}@example.com"

    @pytest.mark.parametrize(
        "payload,reason",
        [
            ({"email": "not-an-email", "password": VALID_PASSWORD, "full_name": "X"}, "email"),
            ({"email": "a@b.com", "password": "short1", "full_name": "X"}, "password len"),
            ({"email": "a@b.com", "password": "nodigitshere", "full_name": "X"}, "password strength"),
            ({"email": "a@b.com", "password": VALID_PASSWORD, "full_name": "  "}, "blank name"),
            ({"email": "a@b.com", "password": VALID_PASSWORD}, "missing name"),
        ],
    )
    async def test_invalid_payloads_return_422(
        self, client: AsyncClient, payload: dict, reason: str
    ):
        response = await client.post("/api/v1/auth/register", json=payload)
        assert response.status_code == 422, f"{reason}: {response.text}"
        body = response.json()
        assert body["success"] is False
        assert body["error_code"] == "validation_error"
        assert isinstance(body["details"], list) and body["details"]


# ------------------------------------------------------------------------ login
class TestLogin:
    async def test_login_succeeds_with_correct_credentials(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session, email=f"ok-{uuid.uuid4().hex[:8]}@example.com")
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": user.email, "password": VALID_PASSWORD},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["user"]["id"] == str(user.id)
        assert data["tokens"]["access_token"]
        assert VALID_PASSWORD not in response.text

    async def test_login_wrong_password_returns_401(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session, email=f"wp-{uuid.uuid4().hex[:8]}@example.com")
        response = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "Wr0ngPassword"}
        )
        assert response.status_code == 401
        assert response.json()["error_code"] == "invalid_credentials"

    async def test_login_unknown_email_returns_identical_401(
        self, client: AsyncClient, session: AsyncSession
    ):
        """Unknown email and wrong password must be indistinguishable."""
        user = await _make_user(session, email=f"known-{uuid.uuid4().hex[:8]}@example.com")
        wrong_password = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "Wr0ngPassword"}
        )
        unknown_email = await client.post(
            "/api/v1/auth/login",
            json={"email": f"ghost-{uuid.uuid4().hex[:8]}@example.com", "password": VALID_PASSWORD},
        )
        assert wrong_password.status_code == unknown_email.status_code == 401
        assert wrong_password.json() == unknown_email.json()

    async def test_inactive_account_cannot_log_in(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(
            session, email=f"off-{uuid.uuid4().hex[:8]}@example.com", is_active=False
        )
        response = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": VALID_PASSWORD}
        )
        assert response.status_code == 403
        assert response.json()["error_code"] == "account_inactive"


# --------------------------------------------------------------------- refresh
class TestRefresh:
    async def test_refresh_returns_a_new_token_pair(self, client: AsyncClient):
        data = await register_via_api(client)
        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": data["tokens"]["refresh_token"]},
        )
        assert response.status_code == 200
        new_tokens = response.json()["data"]["tokens"]
        assert new_tokens["access_token"]
        assert new_tokens["refresh_token"] != data["tokens"]["refresh_token"]

    async def test_refresh_works_end_to_end_into_a_protected_route(
        self, client: AsyncClient
    ):
        """register -> login -> refresh -> access a protected route."""
        registered = await register_via_api(client)

        login = await client.post(
            "/api/v1/auth/login",
            json={
                "email": registered["user"]["email"],
                "password": VALID_PASSWORD,
            },
        )
        assert login.status_code == 200
        tokens = login.json()["data"]["tokens"]

        refreshed = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert refreshed.status_code == 200
        new_access = refreshed.json()["data"]["tokens"]["access_token"]

        protected = await client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {new_access}"}
        )
        assert protected.status_code == 200
        assert protected.json()["data"]["email"] == registered["user"]["email"]

    async def test_replayed_refresh_token_is_rejected(
        self, client: AsyncClient, session: AsyncSession
    ):
        data = await register_via_api(client)
        original = data["tokens"]["refresh_token"]

        first = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert first.status_code == 200

        replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
        assert replay.status_code == 401
        assert replay.json()["error_code"] == "refresh_token_revoked"

    async def test_refresh_token_cannot_be_used_as_an_access_token(
        self, client: AsyncClient, session: AsyncSession
    ):
        data = await register_via_api(client)
        response = await client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {data['tokens']['refresh_token']}"},
        )
        assert response.status_code == 401

    async def test_access_token_cannot_be_used_to_refresh(self, client: AsyncClient):
        data = await register_via_api(client)
        response = await client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": data["tokens"]["access_token"]},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------- logout
class TestLogout:
    async def test_logout_revokes_the_refresh_token(self, client: AsyncClient):
        data = await register_via_api(client)
        refresh_token = data["tokens"]["refresh_token"]

        logout = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
        assert logout.status_code == 200

        reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        assert reuse.status_code == 401

    async def test_logout_is_idempotent(self, client: AsyncClient):
        data = await register_via_api(client)
        payload = {"refresh_token": data["tokens"]["refresh_token"]}
        assert (await client.post("/api/v1/auth/logout", json=payload)).status_code == 200
        assert (await client.post("/api/v1/auth/logout", json=payload)).status_code == 200

    async def test_logout_all_revokes_every_session(self, client: AsyncClient):
        first = await register_via_api(client)
        second_login = await client.post(
            "/api/v1/auth/login",
            json={"email": first["user"]["email"], "password": VALID_PASSWORD},
        )
        second_refresh = second_login.json()["data"]["tokens"]["refresh_token"]

        headers = {"Authorization": f"Bearer {first['tokens']['access_token']}"}
        response = await client.post("/api/v1/auth/logout-all", headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["sessions_revoked"] >= 2

        for token in (first["tokens"]["refresh_token"], second_refresh):
            reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": token})
            assert reuse.status_code == 401


# ------------------------------------------------------------- protected route
class TestProtectedRoutes:
    async def test_users_me_without_token_returns_401(self, client: AsyncClient):
        response = await client.get("/api/v1/users/me")
        assert response.status_code == 401
        body = response.json()
        assert body["success"] is False
        assert body["error_code"] == "missing_credentials"

    @pytest.mark.parametrize(
        "header",
        ["Bearer not-a-jwt", "Bearer ", "notbearer abc.def.ghi", "Basic dXNlcjpwYXNz"],
    )
    async def test_users_me_with_bad_token_returns_401(
        self, client: AsyncClient, header: str
    ):
        response = await client.get("/api/v1/users/me", headers={"Authorization": header})
        assert response.status_code == 401

    async def test_tampered_token_is_rejected(self, client: AsyncClient):
        data = await register_via_api(client)
        token = data["tokens"]["access_token"]
        head, payload, signature = token.split(".")
        forged = f"{head}.{payload}.{'A' * len(signature)}"
        response = await client.get(
            "/api/v1/users/me", headers={"Authorization": f"Bearer {forged}"}
        )
        assert response.status_code == 401

    async def test_auth_me_returns_the_caller(self, client: AsyncClient):
        data = await register_via_api(client)
        headers = {"Authorization": f"Bearer {data['tokens']['access_token']}"}
        response = await client.get("/api/v1/auth/me", headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["id"] == data["user"]["id"]

    async def test_inactive_user_token_is_rejected_after_deactivation(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session, email=f"deact-{uuid.uuid4().hex[:8]}@example.com")
        headers = await auth_header(client, email=user.email)

        assert (await client.get("/api/v1/users/me", headers=headers)).status_code == 200

        repo = UserRepository(session)
        stored = await repo.get_by_id(user.id)
        stored.is_active = False
        await session.commit()

        response = await client.get("/api/v1/users/me", headers=headers)
        assert response.status_code == 403
        assert response.json()["error_code"] == "account_inactive"


# ------------------------------------------------------ password reset / email
class TestPasswordReset:
    async def test_forgot_password_is_neutral_for_unknown_email(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": f"nobody-{uuid.uuid4().hex[:8]}@example.com"},
        )
        assert response.status_code == 200
        assert "sent" in response.json()["message"].lower()

    async def test_forgot_password_does_not_reveal_account_existence(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session, email=f"exists-{uuid.uuid4().hex[:8]}@example.com")
        known = await client.post("/api/v1/auth/forgot-password", json={"email": user.email})
        unknown = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": f"ghost-{uuid.uuid4().hex[:8]}@example.com"},
        )
        assert known.status_code == unknown.status_code == 200
        assert known.json()["message"] == unknown.json()["message"]

    async def test_reset_password_works_and_revokes_sessions(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session, email=f"rst-{uuid.uuid4().hex[:8]}@example.com")
        # Read the email once, up front: resetting the password updates the row,
        # which expires this ORM instance, so touching user.email afterwards
        # would trigger a lazy reload in a sync context (MissingGreenlet).
        email = user.email
        login = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
        )
        old_refresh = login.json()["data"]["tokens"]["refresh_token"]

        # The raw token is only ever returned by the service in dev, so read it
        # from the stored digest by issuing a reset through the service.
        from app.services.auth_service import AuthService

        issued = await AuthService(session).request_password_reset(email)
        assert issued.raw_token is not None

        reset = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": issued.raw_token, "new_password": "N3wStr0ngPass"},
        )
        assert reset.status_code == 200

        old_login = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": VALID_PASSWORD}
        )
        assert old_login.status_code == 401

        new_login = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": "N3wStr0ngPass"}
        )
        assert new_login.status_code == 200

        reuse = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
        assert reuse.status_code == 401

    async def test_reset_password_rejects_bad_token(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": "totally-made-up", "new_password": "N3wStr0ngPass"},
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "invalid_reset_token"

    async def test_reset_password_token_is_single_use(
        self, client: AsyncClient, session: AsyncSession
    ):
        from app.services.auth_service import AuthService

        user = await _make_user(session, email=f"once-{uuid.uuid4().hex[:8]}@example.com")
        issued = await AuthService(session).request_password_reset(user.email)
        payload = {"token": issued.raw_token, "new_password": "N3wStr0ngPass"}

        assert (await client.post("/api/v1/auth/reset-password", json=payload)).status_code == 200
        assert (await client.post("/api/v1/auth/reset-password", json=payload)).status_code == 422


class TestChangePassword:
    async def test_change_password_requires_the_current_one(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session, email=f"chg-{uuid.uuid4().hex[:8]}@example.com")
        headers = await auth_header(client, email=user.email)
        response = await client.post(
            "/api/v1/users/me/change-password",
            json={"current_password": "Wr0ngPassword", "new_password": "N3wStr0ngPass"},
            headers=headers,
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "incorrect_current_password"

    async def test_change_password_succeeds(self, client: AsyncClient, session: AsyncSession):
        user = await _make_user(session, email=f"chg2-{uuid.uuid4().hex[:8]}@example.com")
        headers = await auth_header(client, email=user.email)
        response = await client.post(
            "/api/v1/users/me/change-password",
            json={"current_password": VALID_PASSWORD, "new_password": "N3wStr0ngPass"},
            headers=headers,
        )
        assert response.status_code == 200
        assert VALID_PASSWORD not in response.text

        relogin = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "N3wStr0ngPass"}
        )
        assert relogin.status_code == 200


class TestEmailVerification:
    async def test_verify_email_marks_the_account_verified(
        self, client: AsyncClient, session: AsyncSession
    ):
        from app.services.auth_service import AuthService

        user = await _make_user(
            session, email=f"ver-{uuid.uuid4().hex[:8]}@example.com", is_email_verified=False
        )
        service = AuthService(session)
        token = await service.request_email_verification(user)

        response = await client.post("/api/v1/auth/verify-email", json={"token": token})
        assert response.status_code == 200

        await session.refresh(user)
        assert user.is_email_verified is True

    async def test_verify_email_rejects_an_unknown_token(self, client: AsyncClient):
        response = await client.post(
            "/api/v1/auth/verify-email", json={"token": "nope-not-real"}
        )
        assert response.status_code == 422
        assert response.json()["error_code"] == "invalid_verification_token"

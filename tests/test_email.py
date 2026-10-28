"""Phase 7 email tests against the mock transport.

The mock outbox is the observable boundary: registration, resend, and password
reset must each produce exactly one message for the right recipient, and the
token in that message must complete the flow.
"""
from __future__ import annotations

import re
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_email_service
from app.main import app
from app.services.email_service import (
    EmailMessage,
    MockEmailService,
    reset_email_service,
)
from tests.conftest import (
    VALID_PASSWORD,
    _make_user,
    auth_header_for,
)

pytestmark = pytest.mark.asyncio

TOKEN_PATTERN = re.compile(r"token=([A-Za-z0-9_\-]+)")


def _outbox() -> MockEmailService:
    service = get_email_service()
    assert isinstance(service, MockEmailService), "tests expect the mock transport"
    return service


def _token(message: EmailMessage) -> str:
    match = TOKEN_PATTERN.search(message.text)
    assert match is not None, f"no token in email body: {message.text!r}"
    return match.group(1)


@pytest.fixture(autouse=True)
def clean_outbox():
    reset_email_service()
    yield
    reset_email_service()


class TestMockTransport:
    async def test_outbox_is_bounded_and_clears(self):
        service = MockEmailService(max_outbox=2)
        for index in range(3):
            await service.send(
                EmailMessage(
                    to=f"user-{index}@example.com",
                    subject=f"Message {index}",
                    text="body",
                )
            )

        assert len(service.outbox) == 2
        assert [message.subject for message in service.outbox] == [
            "Message 1",
            "Message 2",
        ]
        assert service.last_message() is service.outbox[-1]

        service.clear()
        assert service.outbox == ()
        assert service.last_message() is None


class TestRegistrationEmail:
    async def test_register_sends_a_verification_email(self, client: AsyncClient):
        email = f"mail-{uuid.uuid4().hex[:8]}@example.com"
        response = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": VALID_PASSWORD, "full_name": "Mailer"},
        )
        assert response.status_code == 201
        # The token is delivered, not exposed in the response.
        assert "email_verification_token" not in response.json()["data"]

        messages = _outbox().messages_to(email)
        assert len(messages) == 1
        message = messages[0]
        assert message.category == "email_verification"
        assert "verify" in message.subject.lower()
        assert "/verify-email?token=" in message.text

    async def test_verification_token_from_email_completes_the_flow(
        self, client: AsyncClient
    ):
        email = f"verify-{uuid.uuid4().hex[:8]}@example.com"
        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": VALID_PASSWORD, "full_name": "Verifier"},
        )
        token = _token(_outbox().messages_to(email)[0])

        verified = await client.post(
            "/api/v1/auth/verify-email", json={"token": token}
        )
        assert verified.status_code == 200

        headers = {
            "Authorization": f"Bearer {registered.json()['data']['tokens']['access_token']}"
        }
        me = await client.get("/api/v1/auth/me", headers=headers)
        assert me.json()["data"]["is_email_verified"] is True

    async def test_delivery_failure_does_not_break_registration(
        self, client: AsyncClient
    ):
        class _FailingEmailService:
            async def send(self, message: EmailMessage) -> None:
                raise RuntimeError("provider is down")

        app.dependency_overrides[get_email_service] = lambda: _FailingEmailService()
        try:
            response = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"down-{uuid.uuid4().hex[:8]}@example.com",
                    "password": VALID_PASSWORD,
                    "full_name": "Resilient",
                },
            )
        finally:
            app.dependency_overrides.pop(get_email_service, None)

        assert response.status_code == 201
        assert response.json()["data"]["tokens"]["access_token"]


class TestResendVerificationEmail:
    async def test_resend_sends_a_fresh_verification_email(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(
            session,
            email=f"resend-{uuid.uuid4().hex[:8]}@example.com",
            is_email_verified=False,
        )
        headers = await auth_header_for(client, user)

        response = await client.post(
            "/api/v1/auth/resend-verification", headers=headers
        )
        assert response.status_code == 200

        messages = _outbox().messages_to(user.email)
        assert len(messages) == 1
        assert messages[0].category == "email_verification"

    async def test_resend_is_a_no_op_when_already_verified(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session, email=f"done-{uuid.uuid4().hex[:8]}@example.com")
        headers = await auth_header_for(client, user)

        response = await client.post(
            "/api/v1/auth/resend-verification", headers=headers
        )
        assert response.status_code == 200
        assert _outbox().messages_to(user.email) == []


class TestPasswordResetEmail:
    async def test_forgot_password_sends_a_reset_email(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session, email=f"forgot-{uuid.uuid4().hex[:8]}@example.com")

        response = await client.post(
            "/api/v1/auth/forgot-password", json={"email": user.email}
        )
        assert response.status_code == 200

        messages = _outbox().messages_to(user.email)
        assert len(messages) == 1
        assert messages[0].category == "password_reset"
        assert "/reset-password?token=" in messages[0].text

    async def test_forgot_password_sends_nothing_for_an_unknown_email(
        self, client: AsyncClient
    ):
        email = f"ghost-{uuid.uuid4().hex[:8]}@example.com"
        response = await client.post(
            "/api/v1/auth/forgot-password", json={"email": email}
        )
        assert response.status_code == 200
        assert _outbox().messages_to(email) == []

    async def test_reset_token_from_email_completes_the_flow(
        self, client: AsyncClient, session: AsyncSession
    ):
        user = await _make_user(session, email=f"reset-{uuid.uuid4().hex[:8]}@example.com")
        email = user.email

        await client.post("/api/v1/auth/forgot-password", json={"email": email})
        token = _token(_outbox().messages_to(email)[0])

        reset = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "new_password": "N3wStr0ngPass"},
        )
        assert reset.status_code == 200

        login = await client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "N3wStr0ngPass"},
        )
        assert login.status_code == 200

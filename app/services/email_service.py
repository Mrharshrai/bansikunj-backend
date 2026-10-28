"""Transactional email delivery (Phase 7).

Delivery is **mocked for now**. :class:`MockEmailService` records every message
in a bounded, process-local outbox and logs it, so the auth flows can be wired
end to end without SMTP credentials. The public surface is a small
:class:`EmailService` protocol: when a real provider (SMTP or an email API) is
available, implement the protocol and return that instance from
:func:`get_email_service`. Callers do not change.

The mock deliberately keeps raw verification/reset links out of structured log
fields in production; the preview body is only logged outside production so a
developer can complete the flow locally.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from ..core.config import get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """A single transactional message, independent of any transport."""

    to: str
    subject: str
    text: str
    html: str | None = None
    category: str = "transactional"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@runtime_checkable
class EmailService(Protocol):
    """Transport boundary: send one message, or raise if delivery fails."""

    async def send(self, message: EmailMessage) -> None: ...


class MockEmailService:
    """Records messages instead of delivering them.

    The outbox is bounded so a long-lived process cannot grow it without limit;
    the oldest message is dropped first. Tests can inspect it directly through
    :attr:`outbox` or :meth:`messages_to`.
    """

    def __init__(self, *, max_outbox: int = 100) -> None:
        if max_outbox < 1:
            raise ValueError("max_outbox must be at least 1")
        self._outbox: deque[EmailMessage] = deque(maxlen=max_outbox)

    async def send(self, message: EmailMessage) -> None:
        self._outbox.append(message)
        logger.info(
            "email.sent",
            backend="mock",
            to=message.to,
            subject=message.subject,
            category=message.category,
        )
        if not get_settings().is_production:
            # Local development convenience: shows the verification/reset link
            # that a real transport would have delivered.
            logger.info(
                "email.mock_preview",
                to=message.to,
                subject=message.subject,
                body=message.text,
            )

    @property
    def outbox(self) -> tuple[EmailMessage, ...]:
        return tuple(self._outbox)

    def messages_to(self, address: str) -> list[EmailMessage]:
        target = address.strip().lower()
        return [message for message in self._outbox if message.to.lower() == target]

    def last_message(self) -> EmailMessage | None:
        return self._outbox[-1] if self._outbox else None

    def clear(self) -> None:
        self._outbox.clear()


_email_service: EmailService = MockEmailService()


def get_email_service() -> EmailService:
    """Return the process-wide email transport.

    Phase 7 is mock-only. Replace the returned instance with an SMTP/API-backed
    implementation when real credentials are available.
    """
    return _email_service


def reset_email_service() -> None:
    """Clear the mock outbox. Intended for development and tests."""
    clear = getattr(_email_service, "clear", None)
    if clear is not None:
        clear()


def _link(frontend_url: str, path: str, token: str) -> str:
    return f"{frontend_url.rstrip('/')}{path}?token={token}"


def verification_email(
    *,
    to: str,
    full_name: str,
    token: str,
    frontend_url: str,
    expires_in: str,
) -> EmailMessage:
    greeting = f"Hi {full_name}," if full_name else "Hi,"
    link = _link(frontend_url, "/verify-email", token)
    text = (
        f"{greeting}\n\n"
        "Welcome to Bansikunj. Confirm your email address to finish setting up "
        "your account:\n\n"
        f"{link}\n\n"
        f"This link expires in {expires_in}. If you did not create an account, "
        "you can ignore this email.\n"
    )
    return EmailMessage(
        to=to,
        subject="Verify your Bansikunj email",
        text=text,
        category="email_verification",
    )


def password_reset_email(
    *,
    to: str,
    full_name: str,
    token: str,
    frontend_url: str,
    expires_in: str,
) -> EmailMessage:
    greeting = f"Hi {full_name}," if full_name else "Hi,"
    link = _link(frontend_url, "/reset-password", token)
    text = (
        f"{greeting}\n\n"
        "We received a request to reset your Bansikunj password. Use the link "
        f"below within {expires_in}:\n\n"
        f"{link}\n\n"
        "If you did not request this, you can ignore this email; your password "
        "has not been changed.\n"
    )
    return EmailMessage(
        to=to,
        subject="Reset your Bansikunj password",
        text=text,
        category="password_reset",
    )

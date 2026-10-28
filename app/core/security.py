"""Password hashing and JWT issuance/verification.

Two token types are issued:

* **access**  - short-lived, sent as ``Authorization: Bearer <token>`` on every
  request. Stateless: verified purely by signature.
* **refresh** - long-lived, stored hashed in the ``refresh_tokens`` table so it
  can be revoked on logout and rotated on refresh.

Refresh tokens are *not* accepted as access tokens: the ``typ`` claim is checked,
so a stolen refresh token cannot be replayed against protected endpoints.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .config import get_settings
from .logging import get_logger

logger = get_logger(__name__)

#: Argon2id is the recommended variant; cost parameters come from settings.
_hasher = PasswordHasher()


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(Exception):
    """Raised when a token is malformed, tampered with, or of the wrong type."""


def utcnow() -> datetime:
    """Timezone-aware current time (all timestamps use UTC)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
#  Password hashing
# ---------------------------------------------------------------------------

def _configured_hasher() -> PasswordHasher:
    """Build a hasher from the current settings."""
    settings = get_settings()
    return PasswordHasher(
        time_cost=settings.PASSWORD_ARGON2_TIME_COST,
        memory_cost=settings.PASSWORD_ARGON2_MEMORY_COST_KIB,
        parallelism=settings.PASSWORD_ARGON2_PARALLELISM,
    )


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return _configured_hasher().hash(password)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Verification. Never raises on a malformed or empty stored hash."""
    if not hashed:
        return False
    try:
        _hasher.verify(hashed, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def password_needs_rehash(hashed: str) -> bool:
    """True when a stored hash used weaker parameters than current settings.

    Argon2 embeds its cost parameters in the hash string, so
    ``check_needs_rehash`` compares the stored values against those of the
    hasher it is called on - no explicit parameters are passed.
    """
    if not hashed:
        return True
    try:
        return _configured_hasher().check_needs_rehash(hashed)
    except InvalidHashError:
        return True


# ---------------------------------------------------------------------------
#  Opaque tokens (email verification / password reset)
# ---------------------------------------------------------------------------

def generate_secure_token(nbytes: int = 32) -> str:
    """URL-safe random token for email-verification and password-reset links."""
    return secrets.token_urlsafe(nbytes)


def hash_opaque_token(token: str) -> str:
    """SHA-256 a single-use token so the raw value is never stored."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(candidate: str, stored_hash: str) -> bool:
    """Constant-time compare of a presented token against its stored hash."""
    return hmac.compare_digest(hash_opaque_token(candidate), stored_hash)


# ---------------------------------------------------------------------------
#  JWT
# ---------------------------------------------------------------------------

def _encode(subject: str, token_type: TokenType, expires_delta: timedelta,
            extra_claims: dict[str, Any] | None = None) -> tuple[str, datetime, str]:
    """Build a signed JWT. Returns ``(token, expires_at, jti)``."""
    settings = get_settings()
    issued_at = utcnow()
    expires_at = issued_at + expires_delta
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type.value,
        "jti": jti,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(
        payload, settings.JWT_SECRET_KEY.get_secret_value(), algorithm=settings.JWT_ALGORITHM
    )
    return token, expires_at, jti


def create_access_token(
    user_id: str | uuid.UUID,
    *,
    role: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, datetime]:
    """Issue an access token. Returns ``(token, expires_at)``."""
    settings = get_settings()
    claims = {"role": role} if role else None
    if extra_claims:
        claims = {**(claims or {}), **extra_claims}
    delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    token, expires_at, _jti = _encode(str(user_id), TokenType.ACCESS, delta, claims)
    return token, expires_at


def create_refresh_token(user_id: str | uuid.UUID) -> tuple[str, datetime, str]:
    """Issue a refresh token. Returns ``(token, expires_at, jti)``."""
    settings = get_settings()
    delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    token, expires_at, jti = _encode(str(user_id), TokenType.REFRESH, delta)
    return token, expires_at, jti


def decode_token(token: str, *, expected_type: TokenType) -> dict[str, Any]:
    """Verify signature, expiry, and token type.

    Raises:
        TokenError: on any failure. Callers map this to a 401.
    """
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.PyJWTError as exc:
        # Deliberately vague: never reveal whether a signature or a claim failed.
        raise TokenError("Token is invalid") from exc

    if payload.get("typ") != expected_type.value:
        raise TokenError(
            f"Expected a {expected_type.value} token but received "
            f"{payload.get('typ')!r}"
        )
    return payload


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify a token is a valid, unexpired access token."""
    return decode_token(token, expected_type=TokenType.ACCESS)


def token_expiry_from_payload(payload: dict[str, Any]) -> datetime:
    """Convert a JWT ``exp`` claim to a timezone-aware datetime."""
    return datetime.fromtimestamp(payload["exp"], tz=timezone.utc)

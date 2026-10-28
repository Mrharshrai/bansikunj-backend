"""Authentication business logic.

Rules enforced here and nowhere else:

* Role is always assigned server-side (``CUSTOMER``); it is never read from input.
* Passwords are verified against Argon2id hashes and rehashed when parameters change.
* Login failures are deliberately indistinguishable (same message, comparable
  timing) whether the email is unknown or the password is wrong.
* ``forgot-password`` always reports success so it cannot be used to enumerate
  registered email addresses.
* Single-use tokens are stored as SHA-256 digests and looked up by that digest,
  so a reset link is a direct index hit rather than a table scan.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.exceptions import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from ..core.logging import get_logger
from ..core.security import (
    TokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_token,
    generate_secure_token,
    hash_opaque_token,
    hash_password,
    password_needs_rehash,
    tokens_match,
    utcnow,
    verify_password,
)
from ..models.user import Role, User
from ..repositories.refresh_token_repository import RefreshTokenRepository
from ..repositories.user_repository import UserRepository
from ..schemas.auth import AuthTokenPair, RegisterRequest
from .email_service import (
    EmailMessage,
    EmailService,
    get_email_service,
    password_reset_email,
    verification_email,
)

logger = get_logger(__name__)

#: How long an email-verification or password-reset link stays valid.
EMAIL_VERIFICATION_TTL = timedelta(hours=24)
PASSWORD_RESET_TTL = timedelta(minutes=30)

#: One message for every credential failure, so the endpoint is not an oracle.
INVALID_CREDENTIALS = "Invalid email or password"

#: Generic response reused by endpoints that must not leak account existence.
NEUTRAL_RESPONSE = "If an account exists for that email, a link has been sent."


@dataclass(slots=True)
class IssuedTokens:
    """Token pair plus the ``jti`` of the refresh token that was persisted."""

    pair: AuthTokenPair
    refresh_jti: str


@dataclass(slots=True)
class RegistrationResult:
    """New account, its tokens, and the raw verification token for delivery."""

    user: User
    tokens: IssuedTokens
    email_verification_token: str


@dataclass(slots=True)
class SingleUseEmail:
    """A raw single-use email plus the owning user, if one matched."""

    user: User | None
    raw_token: str | None
    expires_at: datetime | None


class AuthService:
    def __init__(
        self, session: AsyncSession, email_service: EmailService | None = None
    ) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.refresh_tokens = RefreshTokenRepository(session)
        # Defaulting here keeps direct construction (tests, scripts) working
        # while the route layer injects the same process-wide transport.
        self.email_service = email_service or get_email_service()

    # ------------------------------------------------------------------ issue
    async def _issue_tokens(
        self,
        user: User,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> IssuedTokens:
        settings = get_settings()
        access_token, access_exp = create_access_token(user.id, role=user.role.value)
        refresh_token, refresh_exp, refresh_jti = create_refresh_token(user.id)
        await self.refresh_tokens.create(
            user.id,
            refresh_jti,
            refresh_exp,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        pair = AuthTokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            expires_at=access_exp,
        )
        return IssuedTokens(pair=pair, refresh_jti=refresh_jti)

    # --------------------------------------------------------------- register
    async def register(self, payload: RegisterRequest) -> RegistrationResult:
        """Create a customer account. The role is fixed, never client-supplied."""
        email = str(payload.email).lower()
        if await self.users.email_exists(email):
            raise ConflictError(
                "An account with this email already exists",
                error_code="email_already_registered",
            )

        user = await self.users.create(
            email=email,
            full_name=payload.full_name,
            phone=payload.phone,
            role=Role.CUSTOMER,
            hashed_password=hash_password(payload.password),
            is_active=True,
            is_email_verified=False,
        )
        verification_token = await self._issue_email_verification_token(user)
        await self._send_verification_email(user, verification_token)
        tokens = await self._issue_tokens(user)
        logger.info("auth.registered", user_id=str(user.id))
        return RegistrationResult(
            user=user, tokens=tokens, email_verification_token=verification_token
        )

    # ------------------------------------------------------------------ login
    async def authenticate(
        self, email: str, password: str
    ) -> tuple[User, IssuedTokens]:
        user = await self.users.get_by_email(email)
        if user is None:
            # Hash anyway so a missing account is not detectable by timing.
            verify_password(password, hash_password("timing-equaliser"))
            logger.info("auth.login_failed", reason="unknown_email")
            raise UnauthorizedError(INVALID_CREDENTIALS, error_code="invalid_credentials")

        if not verify_password(password, user.hashed_password):
            logger.info("auth.login_failed", user_id=str(user.id), reason="bad_password")
            raise UnauthorizedError(INVALID_CREDENTIALS, error_code="invalid_credentials")

        if not user.is_active:
            raise ForbiddenError(
                "This account has been deactivated", error_code="account_inactive"
            )

        if password_needs_rehash(user.hashed_password):
            user.hashed_password = hash_password(password)
            await self.session.flush()
            logger.info("auth.password_rehashed", user_id=str(user.id))

        logger.info("auth.login", user_id=str(user.id))
        tokens = await self._issue_tokens(user)
        return user, tokens

    # ---------------------------------------------------------------- refresh
    async def refresh(self, refresh_token: str) -> IssuedTokens:
        """Exchange a refresh token for a new pair, rotating the old one.

        Presenting an already-rotated token is treated as theft: every active
        session belonging to that user is revoked.
        """
        try:
            payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        except TokenError as exc:
            raise UnauthorizedError(
                str(exc), error_code="invalid_refresh_token"
            ) from exc

        jti = payload["jti"]
        record = await self.refresh_tokens.get_active_by_jti(jti)
        if record is None:
            await self._handle_replayed_refresh(payload)
            raise UnauthorizedError(
                "Refresh token is no longer valid", error_code="refresh_token_revoked"
            )

        user = await self.users.get_by_id(record.user_id)
        if user is None:
            raise UnauthorizedError("Account no longer exists", error_code="unknown_user")
        if not user.is_active:
            raise ForbiddenError(
                "This account has been deactivated", error_code="account_inactive"
            )

        tokens = await self._issue_tokens(
            user, user_agent=record.user_agent, ip_address=record.ip_address
        )
        await self.refresh_tokens.mark_replaced(jti, tokens.refresh_jti, utcnow())
        await self.session.flush()
        logger.info("auth.token_refreshed", user_id=str(user.id))
        return tokens

    async def _handle_replayed_refresh(self, payload: dict[str, object]) -> None:
        """Revoke every session for the subject of a replayed refresh token."""
        try:
            user_id = uuid.UUID(str(payload["sub"]))
        except (KeyError, ValueError):
            logger.warning("auth.refresh_replay_unparseable_subject")
            return
        revoked = await self.refresh_tokens.revoke_all_for_user(user_id, utcnow())
        await self.session.flush()
        logger.warning(
            "auth.refresh_token_replay_detected",
            user_id=str(user_id),
            sessions_revoked=revoked,
        )

    # ----------------------------------------------------------------- logout
    async def logout(self, refresh_token: str) -> None:
        """Revoke a single refresh token. Idempotent by design."""
        try:
            payload = decode_token(refresh_token, expected_type=TokenType.REFRESH)
        except TokenError:
            logger.info("auth.logout_with_unusable_token")
            return
        revoked = await self.refresh_tokens.revoke_by_jti(payload["jti"], utcnow())
        await self.session.flush()
        logger.info("auth.logout", revoked=bool(revoked), user_id=payload.get("sub"))

    async def logout_everywhere(self, user_id: uuid.UUID) -> int:
        count = await self.refresh_tokens.revoke_all_for_user(user_id, utcnow())
        await self.session.flush()
        logger.info("auth.logout_all", user_id=str(user_id), sessions_revoked=count)
        return count

    # ------------------------------------------------------- email verification
    async def _issue_email_verification_token(self, user: User) -> str:
        """Store a fresh verification digest and return the raw token."""
        raw = generate_secure_token()
        user.email_verification_token_hash = hash_opaque_token(raw)
        user.email_verification_expires_at = utcnow() + EMAIL_VERIFICATION_TTL
        await self.session.flush()
        return raw

    async def request_email_verification(self, user: User) -> str:
        raw = await self._issue_email_verification_token(user)
        await self._send_verification_email(user, raw)
        logger.info("auth.email_verification_requested", user_id=str(user.id))
        return raw

    async def verify_email(self, raw_token: str) -> User:
        user = await self._user_by_valid_single_use_token(
            User.email_verification_token_hash,
            User.email_verification_expires_at,
            raw_token,
        )
        if user is None:
            raise ValidationError(
                "Verification link is invalid or has expired",
                error_code="invalid_verification_token",
            )
        user.is_email_verified = True
        user.email_verification_token_hash = None
        user.email_verification_expires_at = None
        await self.session.flush()
        logger.info("auth.email_verified", user_id=str(user.id))
        return user

    # ------------------------------------------------------------ password reset
    async def request_password_reset(self, email: str) -> SingleUseEmail:
        """Issue a reset token, or report nothing for an unknown address."""
        user = await self.users.get_by_email(email)
        if user is None or not user.is_active:
            logger.info("auth.password_reset_for_inactive_or_unknown")
            return SingleUseEmail(user=None, raw_token=None, expires_at=None)

        raw = generate_secure_token()
        user.password_reset_token_hash = hash_opaque_token(raw)
        user.password_reset_expires_at = utcnow() + PASSWORD_RESET_TTL
        await self.session.flush()
        await self._send_password_reset_email(user, raw)
        logger.info("auth.password_reset_requested", user_id=str(user.id))
        return SingleUseEmail(
            user=user, raw_token=raw, expires_at=user.password_reset_expires_at
        )

    async def reset_password(self, raw_token: str, new_password: str) -> User:
        user = await self._user_by_valid_single_use_token(
            User.password_reset_token_hash,
            User.password_reset_expires_at,
            raw_token,
        )
        if user is None:
            raise ValidationError(
                "Reset link is invalid or has expired", error_code="invalid_reset_token"
            )
        user.hashed_password = hash_password(new_password)
        user.password_reset_token_hash = None
        user.password_reset_expires_at = None
        await self.session.flush()
        # A password change invalidates every existing session.
        await self.refresh_tokens.revoke_all_for_user(user.id, utcnow())
        logger.info("auth.password_reset", user_id=str(user.id))
        return user

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> User:
        if not verify_password(current_password, user.hashed_password):
            raise ValidationError(
                "Current password is incorrect", error_code="incorrect_current_password"
            )
        user.hashed_password = hash_password(new_password)
        await self.session.flush()
        await self.refresh_tokens.revoke_all_for_user(user.id, utcnow())
        logger.info("auth.password_changed", user_id=str(user.id))
        return user

    # ------------------------------------------------------------------ email
    async def _send_verification_email(self, user: User, raw_token: str) -> bool:
        settings = get_settings()
        return await self._deliver(
            verification_email(
                to=user.email,
                full_name=user.full_name,
                token=raw_token,
                frontend_url=settings.FRONTEND_URL,
                expires_in="24 hours",
            ),
            event="email_verification",
            user=user,
        )

    async def _send_password_reset_email(self, user: User, raw_token: str) -> bool:
        settings = get_settings()
        return await self._deliver(
            password_reset_email(
                to=user.email,
                full_name=user.full_name,
                token=raw_token,
                frontend_url=settings.FRONTEND_URL,
                expires_in="30 minutes",
            ),
            event="password_reset",
            user=user,
        )

    async def _deliver(self, message: EmailMessage, *, event: str, user: User) -> bool:
        """Send an email without letting a transport failure break auth.

        A registration or reset request must still succeed if the provider is
        briefly unavailable; the failure is logged for an operator to retry.
        """
        try:
            await self.email_service.send(message)
        except Exception:  # noqa: BLE001 - delivery is best-effort by design
            logger.exception(
                "auth.email_delivery_failed", email_event=event, user_id=str(user.id)
            )
            return False
        logger.info("auth.email_sent", email_event=event, user_id=str(user.id))
        return True

    # ------------------------------------------------------------------ helper
    async def _user_by_valid_single_use_token(
        self,
        hash_column: object,
        expiry_column: object,
        raw_token: str,
    ) -> User | None:
        """Resolve a single-use token to its user via a direct digest lookup."""
        digest = hash_opaque_token(raw_token)
        result = await self.session.execute(
            select(User).where(hash_column == digest)  # type: ignore[operator]
        )
        user = result.scalar_one_or_none()
        if user is None:
            return None
        expiry = getattr(user, str(expiry_column.key))  # type: ignore[union-attr]
        if expiry is not None and expiry < utcnow():
            return None
        # Defence in depth: the digest match is authoritative, but confirm the
        # constant-time comparison too in case of an unexpected index state.
        stored = getattr(user, str(hash_column.key))  # type: ignore[union-attr]
        return user if stored and tokens_match(raw_token, stored) else None

    # ------------------------------------------------- access-token validation
    @staticmethod
    def resolve_access_token(token: str) -> uuid.UUID:
        """Decode an access token and return its subject as a UUID."""
        try:
            payload = decode_access_token(token)
        except TokenError as exc:
            raise UnauthorizedError(str(exc), error_code="invalid_token") from exc
        try:
            return uuid.UUID(str(payload["sub"]))
        except (KeyError, ValueError) as exc:
            raise UnauthorizedError(
                "Token is invalid", error_code="invalid_token"
            ) from exc

    async def get_active_user(self, user_id: uuid.UUID) -> User:
        user = await self.users.get_by_id(user_id)
        if user is None:
            raise UnauthorizedError("Account no longer exists", error_code="unknown_user")
        if not user.is_active:
            raise ForbiddenError(
                "This account has been deactivated", error_code="account_inactive"
            )
        return user

    async def require_user(self, user_id: uuid.UUID) -> User:
        user = await self.users.get_by_id(user_id)
        if user is None:
            raise NotFoundError("User not found", error_code="user_not_found")
        return user

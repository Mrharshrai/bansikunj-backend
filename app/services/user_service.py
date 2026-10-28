"""User profile business logic."""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import ConflictError, ValidationError
from ..core.logging import get_logger
from ..models.user import Role, User
from ..repositories.user_repository import UserRepository
from ..schemas.user import UserProfileUpdate

logger = get_logger(__name__)


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)

    async def get_user(self, user_id: uuid.UUID) -> User:
        user = await self.users.get_by_id(user_id)
        if user is None:
            raise ValidationError("User not found", error_code="user_not_found")
        return user

    async def update_profile(
        self, user: User, payload: UserProfileUpdate
    ) -> User:
        """Apply a partial profile update.

        ``model_dump(exclude_unset=True)`` is used so an omitted field is left
        alone rather than nulled, which matters for a PATCH.
        """
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            return user

        new_email = changes.pop("email", None)
        if new_email is not None:
            await self._change_email(user, str(new_email))

        if changes:
            user = await self.users.update(user, **changes)
        await self.session.flush()
        logger.info("user.profile_updated", user_id=str(user.id), fields=sorted(changes))
        return user

    async def _change_email(self, user: User, new_email: str) -> None:
        """Change email and force re-verification of the new address."""
        normalised = new_email.lower()
        if normalised == user.email:
            return
        if await self.users.email_exists(normalised):
            raise ConflictError(
                "An account with this email already exists",
                error_code="email_already_registered",
            )
        user.email = normalised
        user.is_email_verified = False
        user.email_verification_token_hash = None
        user.email_verification_expires_at = None
        logger.info("user.email_changed", user_id=str(user.id))

    async def change_role(self, user: User, role: Role) -> User:
        """Admin-only. Phase 8 wires the endpoint and the audit log entry."""
        previous = user.role
        user = await self.users.set_role(user, role)
        await self.session.flush()
        logger.info(
            "user.role_changed",
            user_id=str(user.id),
            previous_role=str(previous),
            new_role=str(role),
        )
        return user

    async def set_active(self, user: User, is_active: bool) -> User:
        user.is_active = is_active
        await self.session.flush()
        logger.info("user.status_changed", user_id=str(user.id), is_active=is_active)
        return user

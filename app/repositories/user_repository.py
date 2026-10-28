"""User persistence."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.user import Role, User


class UserRepository:
    """CRUD and lookups for :class:`~app.models.user.User`."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **values: object) -> User:
        user = User(**values)
        self.session.add(user)
        await self.session.flush()
        return user

    async def get_by_id(self, user_id: uuid.UUID | str) -> User | None:
        return await self.session.get(User, self._as_uuid(user_id))

    async def get_by_email(self, email: str) -> User | None:
        """Emails are normalised to lowercase on write, so compare lowercased."""
        result = await self.session.execute(
            select(User).where(func.lower(User.email) == email.strip().lower())
        )
        return result.scalar_one_or_none()

    async def email_exists(self, email: str) -> bool:
        result = await self.session.execute(
            select(func.count())
            .select_from(User)
            .where(func.lower(User.email) == email.strip().lower())
        )
        return bool(result.scalar_one())

    async def update(self, user: User, **values: object) -> User:
        for key, value in values.items():
            if value is not None or key in {"phone", "line2"}:
                setattr(user, key, value)
        await self.session.flush()
        return user

    async def set_role(self, user: User, role: Role) -> User:
        await self.session.execute(
            update(User).where(User.id == user.id).values(role=role)
        )
        user.role = role
        await self.session.flush()
        return user

    async def set_password_hash(self, user: User, password_hash: str) -> User:
        user.hashed_password = password_hash
        await self.session.flush()
        return user

    async def count(self) -> int:
        result = await self.session.execute(select(func.count()).select_from(User))
        return int(result.scalar_one())

    @staticmethod
    def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

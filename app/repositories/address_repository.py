"""Address persistence. Every query is filtered by ``user_id``."""
from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.address import Address


class AddressRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, user_id: uuid.UUID, **values: object) -> Address:
        address = Address(user_id=user_id, **values)
        self.session.add(address)
        await self.session.flush()
        return address

    async def get_by_id(
        self, address_id: uuid.UUID | str, user_id: uuid.UUID | str
    ) -> Address | None:
        """Fetch one address *scoped to its owner*.

        Scoping in the query (rather than filtering afterwards) means another
        user's address is indistinguishable from one that does not exist.
        """
        result = await self.session.execute(
            select(Address).where(
                Address.id == self._as_uuid(address_id),
                Address.user_id == self._as_uuid(user_id),
            )
        )
        return result.scalar_one_or_none()

    async def list_for_user(
        self, user_id: uuid.UUID | str, *, limit: int = 50, offset: int = 0
    ) -> list[Address]:
        result = await self.session.execute(
            select(Address)
            .where(Address.user_id == self._as_uuid(user_id))
            .order_by(Address.is_default.desc(), Address.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_user(self, user_id: uuid.UUID | str) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(Address)
            .where(Address.user_id == self._as_uuid(user_id))
        )
        return int(result.scalar_one())

    async def has_any(self, user_id: uuid.UUID | str) -> bool:
        return await self.count_for_user(user_id) > 0

    async def clear_default(self, user_id: uuid.UUID | str) -> None:
        """Demote all of a user's addresses so one can be promoted safely."""
        await self.session.execute(
            update(Address)
            .where(Address.user_id == self._as_uuid(user_id))
            .values(is_default=False)
        )

    async def set_default(self, address_id: uuid.UUID | str, user_id: uuid.UUID | str) -> Address | None:
        await self.clear_default(user_id)
        result = await self.session.execute(
            update(Address)
            .where(
                Address.id == self._as_uuid(address_id),
                Address.user_id == self._as_uuid(user_id),
            )
            .values(is_default=True)
            .returning(Address)
        )
        return result.scalar_one_or_none()

    async def update(self, address: Address, **values: object) -> Address:
        for key, value in values.items():
            setattr(address, key, value)
        await self.session.flush()
        return address

    async def delete(self, address_id: uuid.UUID | str, user_id: uuid.UUID | str) -> bool:
        result = await self.session.execute(
            delete(Address).where(
                Address.id == self._as_uuid(address_id),
                Address.user_id == self._as_uuid(user_id),
            )
        )
        return bool(result.rowcount)

    @staticmethod
    def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

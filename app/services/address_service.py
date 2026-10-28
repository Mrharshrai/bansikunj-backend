"""Address business logic.

Every operation takes the acting ``user_id`` and is scoped to it, so one user
can never read or mutate another user's address.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import NotFoundError, ValidationError
from ..core.logging import get_logger
from ..models.address import Address
from ..repositories.address_repository import AddressRepository
from ..schemas.address import AddressCreate, AddressUpdate

logger = get_logger(__name__)


class AddressService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.addresses = AddressRepository(session)

    async def create(self, user_id: uuid.UUID, payload: AddressCreate) -> Address:
        """Create an address, making it default if it is the user's first."""
        values = payload.model_dump()
        wants_default = bool(values.pop("is_default", False))
        is_first = not await self.addresses.has_any(user_id)

        if wants_default or is_first:
            # The partial unique index allows only one default per user, so the
            # previous default has to be cleared in the same transaction.
            await self.addresses.clear_default(user_id)
            values["is_default"] = True
        else:
            values["is_default"] = False

        address = await self.addresses.create(user_id, **values)
        await self.session.flush()
        logger.info(
            "address.created",
            address_id=str(address.id),
            user_id=str(user_id),
            is_default=address.is_default,
        )
        return address

    async def list_for_user(self, user_id: uuid.UUID) -> list[Address]:
        return await self.addresses.list_for_user(user_id)

    async def get(self, user_id: uuid.UUID, address_id: uuid.UUID | str) -> Address:
        """Fetch one of *this user's* addresses.

        A missing address and someone else's address both raise the same 404, so
        the endpoint cannot be used to probe for valid ids.
        """
        address = await self.addresses.get_by_id(address_id, user_id)
        if address is None:
            raise NotFoundError("Address not found", error_code="address_not_found")
        return address

    async def update(
        self, user_id: uuid.UUID, address_id: uuid.UUID | str, payload: AddressUpdate
    ) -> Address:
        address = await self.get(user_id, address_id)
        changes = payload.model_dump(exclude_unset=True)

        wants_default = changes.pop("is_default", None)
        if wants_default and not address.is_default:
            await self.addresses.clear_default(user_id)
            address.is_default = True
        elif wants_default is False and address.is_default:
            raise ValidationError(
                "Cannot unset the only default address; set another as default first",
                error_code="default_address_required",
            )

        if changes:
            address = await self.addresses.update(address, **changes)
        await self.session.flush()
        logger.info(
            "address.updated",
            address_id=str(address.id),
            user_id=str(user_id),
            fields=sorted(changes),
        )
        return address

    async def set_default(
        self, user_id: uuid.UUID, address_id: uuid.UUID | str
    ) -> Address:
        # Raises 404 when the address is not owned by this user.
        await self.get(user_id, address_id)
        address = await self.addresses.set_default(address_id, user_id)
        if address is None:
            raise NotFoundError("Address not found", error_code="address_not_found")
        await self.session.flush()
        logger.info("address.default_set", address_id=str(address.id), user_id=str(user_id))
        return address

    async def delete(self, user_id: uuid.UUID, address_id: uuid.UUID | str) -> None:
        address = await self.get(user_id, address_id)
        was_default = address.is_default
        deleted = await self.addresses.delete(address_id, user_id)
        if not deleted:
            raise NotFoundError("Address not found", error_code="address_not_found")
        await self.session.flush()

        # Promote another address so the user always has a default.
        if was_default:
            remaining = await self.addresses.list_for_user(user_id, limit=1)
            if remaining:
                await self.addresses.set_default(remaining[0].id, user_id)
                await self.session.flush()
        logger.info("address.deleted", address_id=str(address_id), user_id=str(user_id))

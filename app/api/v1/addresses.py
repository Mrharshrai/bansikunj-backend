"""Address endpoints.

Every handler takes the authenticated user and passes their id into the service,
which is the only place scoping happens. No route accepts a ``user_id`` from the
client, so there is no way to address another user's data.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from ...core.dependencies import CurrentUser, DbSessionDep
from ...core.envelope import SuccessEnvelope
from ...core.logging import get_logger
from ...schemas.address import AddressCreate, AddressOut, AddressUpdate
from ...services.address_service import AddressService

logger = get_logger(__name__)

router = APIRouter(prefix="/addresses", tags=["addresses"])


def get_address_service(session: DbSessionDep) -> AddressService:
    return AddressService(session)


AddressServiceDep = Annotated[AddressService, Depends(get_address_service)]


class SingleAddress(BaseModel):
    """Envelope ``data`` for create/get/update."""

    address: AddressOut


class AddressList(BaseModel):
    """Envelope ``data`` for list."""

    addresses: list[AddressOut]
    total: int


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[SingleAddress],
    summary="Create an address",
)
async def create_address(
    payload: AddressCreate, user: CurrentUser, service: AddressServiceDep
) -> SuccessEnvelope[SingleAddress]:
    """Create an address. A user's first address automatically becomes default."""
    address = await service.create(user.id, payload)
    return SuccessEnvelope[SingleAddress](
        message="Address created successfully",
        data=SingleAddress(address=AddressOut.model_validate(address, from_attributes=True)),
    )


@router.get(
    "", response_model=SuccessEnvelope[AddressList], summary="List the current user's addresses"
)
async def list_addresses(
    user: CurrentUser, service: AddressServiceDep
) -> SuccessEnvelope[AddressList]:
    addresses = await service.list_for_user(user.id)
    return SuccessEnvelope[AddressList](
        message="Addresses retrieved successfully",
        data=AddressList(
            addresses=[
                AddressOut.model_validate(a, from_attributes=True) for a in addresses
            ],
            total=len(addresses),
        ),
    )


@router.get(
    "/{address_id}",
    response_model=SuccessEnvelope[SingleAddress],
    summary="Fetch one address",
)
async def get_address(
    address_id: uuid.UUID, user: CurrentUser, service: AddressServiceDep
) -> SuccessEnvelope[SingleAddress]:
    """Fetch an address.

    Another user's address returns the same 404 as a nonexistent one, so the
    endpoint cannot be used to discover valid ids.
    """
    address = await service.get(user.id, address_id)
    return SuccessEnvelope[SingleAddress](
        message="Address retrieved",
        data=SingleAddress(address=AddressOut.model_validate(address, from_attributes=True)),
    )


@router.put(
    "/{address_id}",
    response_model=SuccessEnvelope[SingleAddress],
    summary="Update an address",
)
async def update_address(
    address_id: uuid.UUID,
    payload: AddressUpdate,
    user: CurrentUser,
    service: AddressServiceDep,
) -> SuccessEnvelope[SingleAddress]:
    address = await service.update(user.id, address_id, payload)
    return SuccessEnvelope[SingleAddress](
        message="Address updated successfully",
        data=SingleAddress(address=AddressOut.model_validate(address, from_attributes=True)),
    )


@router.patch(
    "/{address_id}",
    response_model=SuccessEnvelope[SingleAddress],
    summary="Partially update an address",
)
async def patch_address(
    address_id: uuid.UUID,
    payload: AddressUpdate,
    user: CurrentUser,
    service: AddressServiceDep,
) -> SuccessEnvelope[SingleAddress]:
    """Only fields present in the body are changed."""
    address = await service.update(user.id, address_id, payload)
    return SuccessEnvelope[SingleAddress](
        message="Address updated successfully",
        data=SingleAddress(address=AddressOut.model_validate(address, from_attributes=True)),
    )


@router.post(
    "/{address_id}/default",
    response_model=SuccessEnvelope[SingleAddress],
    summary="Set the default address",
)
async def set_default_address(
    address_id: uuid.UUID, user: CurrentUser, service: AddressServiceDep
) -> SuccessEnvelope[SingleAddress]:
    """Promote this address and demote the user's previous default."""
    address = await service.set_default(user.id, address_id)
    return SuccessEnvelope[SingleAddress](
        message="Default address updated",
        data=SingleAddress(address=AddressOut.model_validate(address, from_attributes=True)),
    )


@router.delete(
    "/{address_id}",
    response_model=SuccessEnvelope[None],
    summary="Delete an address",
)
async def delete_address(
    address_id: uuid.UUID, user: CurrentUser, service: AddressServiceDep
) -> SuccessEnvelope[None]:
    """Delete an address. If it was the default, another is promoted."""
    await service.delete(user.id, address_id)
    return SuccessEnvelope[None](message="Address deleted successfully", data=None)

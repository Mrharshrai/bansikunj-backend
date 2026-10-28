"""Banner endpoints.

Reading is public and only ever returns active banners, so the carousel on the
homepage needs no token. Writing requires the ``admin`` role, applied by the
``require_admin`` dependency rather than by inspecting the user inside the
handler.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from pydantic import BaseModel

from ...core.dependencies import DbSessionDep, OptionalUser, require_admin
from ...core.envelope import SuccessEnvelope
from ...models.user import Role, User
from ...schemas.banner import BannerCreate, BannerOut, BannerUpdate
from ...services.banner_service import BannerService

router = APIRouter(prefix="/banners", tags=["banners"])


def get_banner_service(session: DbSessionDep) -> BannerService:
    return BannerService(session)


BannerServiceDep = Annotated[BannerService, Depends(get_banner_service)]

AdminDep = Annotated[User, Depends(require_admin)]

BannerIdPath = Annotated[
    uuid.UUID,
    Path(description="Banner id. A non-UUID value is a 422, not a 404."),
]


class SingleBanner(BaseModel):
    banner: BannerOut


class BannerList(BaseModel):
    banners: list[BannerOut]
    total: int


@router.get("", response_model=SuccessEnvelope[BannerList], summary="List banners")
async def list_banners(
    service: BannerServiceDep,
    include_inactive: Annotated[
        bool,
        Query(description="Include hidden banners. Requires the admin role."),
    ] = False,
    viewer: OptionalUser = None,
) -> SuccessEnvelope[BannerList]:
    """Banners in display order for the homepage carousel.

    ``include_inactive`` only takes effect for an authenticated admin; for
    anyone else the flag is ignored and active banners are returned, so the
    parameter cannot be used to reveal hidden slides.
    """
    is_admin = viewer is not None and viewer.role == Role.ADMIN
    banners = await service.list_banners(include_inactive=include_inactive and is_admin)
    return SuccessEnvelope[BannerList](
        message="Banners retrieved successfully",
        data=BannerList(
            banners=[BannerOut.from_entity(banner) for banner in banners],
            total=len(banners),
        ),
    )


@router.get(
    "/{banner_id}",
    response_model=SuccessEnvelope[SingleBanner],
    summary="Fetch one banner",
)
async def get_banner(
    banner_id: BannerIdPath,
    service: BannerServiceDep,
    viewer: OptionalUser = None,
) -> SuccessEnvelope[SingleBanner]:
    is_admin = viewer is not None and viewer.role == Role.ADMIN
    banner = await service.get_by_id(banner_id, include_inactive=is_admin)
    return SuccessEnvelope[SingleBanner](
        message="Banner retrieved",
        data=SingleBanner(banner=BannerOut.from_entity(banner)),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[SingleBanner],
    summary="Create a banner",
)
async def create_banner(
    payload: BannerCreate,
    service: BannerServiceDep,
    _: AdminDep,
) -> SuccessEnvelope[SingleBanner]:
    """Create a banner slide. ``image_url`` is required."""
    banner = await service.create(payload)
    return SuccessEnvelope[SingleBanner](
        message="Banner created successfully",
        data=SingleBanner(banner=BannerOut.from_entity(banner)),
    )


@router.put(
    "/{banner_id}",
    response_model=SuccessEnvelope[SingleBanner],
    summary="Replace a banner",
)
async def replace_banner(
    banner_id: BannerIdPath,
    payload: BannerUpdate,
    service: BannerServiceDep,
    _: AdminDep,
) -> SuccessEnvelope[SingleBanner]:
    """Update a banner. Only fields present in the body are changed."""
    banner = await service.update(banner_id, payload)
    return SuccessEnvelope[SingleBanner](
        message="Banner updated successfully",
        data=SingleBanner(banner=BannerOut.from_entity(banner)),
    )


@router.patch(
    "/{banner_id}",
    response_model=SuccessEnvelope[SingleBanner],
    summary="Partially update a banner",
)
async def patch_banner(
    banner_id: BannerIdPath,
    payload: BannerUpdate,
    service: BannerServiceDep,
    _: AdminDep,
) -> SuccessEnvelope[SingleBanner]:
    """Only fields present in the body are changed."""
    banner = await service.update(banner_id, payload)
    return SuccessEnvelope[SingleBanner](
        message="Banner updated successfully",
        data=SingleBanner(banner=BannerOut.from_entity(banner)),
    )


@router.delete(
    "/{banner_id}",
    response_model=SuccessEnvelope[None],
    summary="Delete a banner",
)
async def delete_banner(
    banner_id: BannerIdPath, service: BannerServiceDep, _: AdminDep
) -> SuccessEnvelope[None]:
    """Hard delete. Inactive banners can be deleted too."""
    await service.delete(banner_id)
    return SuccessEnvelope[None](message="Banner deleted successfully", data=None)

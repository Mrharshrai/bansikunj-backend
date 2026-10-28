"""Banner business logic.

Public callers only ever see active banners. There is no slug or name to keep
unique, so the service is mostly NotFound handling plus the invariant that a
banner always has an image.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import NotFoundError
from ..core.logging import get_logger
from ..models.banner import Banner
from ..repositories.banner_repository import BannerRepository
from ..schemas.banner import BannerCreate, BannerUpdate

logger = get_logger(__name__)


class BannerService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.banners = BannerRepository(session)

    # ------------------------------------------------------------------ read
    async def list_banners(self, *, include_inactive: bool = False) -> list[Banner]:
        return await self.banners.list_all(include_inactive=include_inactive)

    async def get_by_id(
        self, banner_id: uuid.UUID | str, *, include_inactive: bool = False
    ) -> Banner:
        banner = await self.banners.get_by_id(
            banner_id, include_inactive=include_inactive
        )
        if banner is None:
            raise NotFoundError("Banner not found", error_code="banner_not_found")
        return banner

    # ----------------------------------------------------------------- write
    async def create(self, payload: BannerCreate) -> Banner:
        banner = await self.banners.create(**payload.model_dump())
        logger.info("banner.created", banner_id=str(banner.id), title=banner.title)
        return banner

    async def update(
        self, banner_id: uuid.UUID | str, payload: BannerUpdate
    ) -> Banner:
        banner = await self.get_by_id(banner_id, include_inactive=True)
        changes = payload.model_dump(exclude_unset=True)

        if changes:
            banner = await self.banners.update(banner, **changes)
        logger.info(
            "banner.updated",
            banner_id=str(banner.id),
            fields=sorted(changes),
        )
        return banner

    async def delete(self, banner_id: uuid.UUID | str) -> None:
        banner = await self.get_by_id(banner_id, include_inactive=True)
        await self.banners.delete(banner)
        logger.info("banner.deleted", banner_id=str(banner.id), title=banner.title)


__all__ = ["BannerService"]

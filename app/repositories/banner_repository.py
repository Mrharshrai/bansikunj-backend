"""Banner persistence."""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.banner import Banner


class BannerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **values: object) -> Banner:
        banner = Banner(**values)
        self.session.add(banner)
        await self.session.flush()
        # ``created_at``/``updated_at`` are server defaults, so they are expired
        # after the flush. Reading one in an async context without awaiting a
        # load raises MissingGreenlet, which surfaces as a 503.
        await self.session.refresh(banner)
        return banner

    async def get_by_id(
        self, banner_id: uuid.UUID | str, *, include_inactive: bool = False
    ) -> Banner | None:
        result = await self.session.execute(
            select(Banner).where(
                Banner.id == self._as_uuid(banner_id),
                *([] if include_inactive else (Banner.is_active.is_(True),)),
            )
        )
        return result.scalar_one_or_none()

    async def list_all(self, *, include_inactive: bool = False) -> list[Banner]:
        """Banners in display order; only active ones unless asked otherwise."""
        result = await self.session.execute(
            select(Banner)
            .where(*([] if include_inactive else (Banner.is_active.is_(True),)))
            .order_by(Banner.display_order.asc(), Banner.title.asc(), Banner.id.asc())
        )
        return list(result.scalars().all())

    async def update(self, banner: Banner, **values: object) -> Banner:
        for key, value in values.items():
            setattr(banner, key, value)
        await self.session.flush()
        # ``updated_at`` uses ``onupdate=func.now()``: the database computes it,
        # so the instance is expired after flushing and must be reloaded before
        # the response is serialised.
        await self.session.refresh(banner)
        return banner

    async def delete(self, banner: Banner) -> None:
        await self.session.delete(banner)
        await self.session.flush()

    @staticmethod
    def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

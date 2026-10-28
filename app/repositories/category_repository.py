"""Category persistence."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.category import Category
from ..models.product import Product


class CategoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **values: object) -> Category:
        category = Category(**values)
        self.session.add(category)
        await self.session.flush()
        # ``created_at``/``updated_at`` are server defaults, so they are expired
        # after the flush. Reading one in an async context without awaiting a
        # load raises MissingGreenlet, which surfaces as a 503.
        await self.session.refresh(category)
        return category

    async def get_by_id(self, category_id: uuid.UUID | str) -> Category | None:
        result = await self.session.execute(
            select(Category).where(Category.id == self._as_uuid(category_id))
        )
        return result.scalar_one_or_none()

    async def get_by_slug(
        self, slug: str, *, include_inactive: bool = False
    ) -> Category | None:
        result = await self.session.execute(
            select(Category).where(
                Category.slug == slug.strip().lower(),
                *([] if include_inactive else (Category.is_active.is_(True),)),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> Category | None:
        result = await self.session.execute(
            select(Category).where(func.lower(Category.name) == name.strip().lower())
        )
        return result.scalar_one_or_none()

    async def slug_exists(self, slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        """Whether ``slug`` is taken. ``exclude_id`` lets a product keep its own slug."""
        query = select(Category.id).where(Category.slug == slug)
        if exclude_id is not None:
            query = query.where(Category.id != exclude_id)
        return (await self.session.execute(query.limit(1))).first() is not None

    async def name_exists(
        self, name: str, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        query = select(Category.id).where(func.lower(Category.name) == name.strip().lower())
        if exclude_id is not None:
            query = query.where(Category.id != exclude_id)
        return (await self.session.execute(query.limit(1))).first() is not None

    async def list_all(
        self, *, include_inactive: bool = False
    ) -> list[tuple[Category, int]]:
        """Categories in display order, with a product count per category.

        The count comes from one grouped join rather than lazy-loading
        ``category.products`` for every row.
        """
        product_count = (
            select(
                Product.category_id.label("category_id"),
                func.count(Product.id).label("product_count"),
            )
            .where(Product.is_active.is_(True))
            .group_by(Product.category_id)
            .subquery()
        )
        result = await self.session.execute(
            select(Category, func.coalesce(product_count.c.product_count, 0))
            .outerjoin(product_count, product_count.c.category_id == Category.id)
            .where(*([] if include_inactive else (Category.is_active.is_(True),)))
            .order_by(Category.display_order.asc(), Category.name.asc())
        )
        # Unwrap each Row: callers build the response from the ORM entity via
        # attribute access (``category.id``), which a Row does not support
        # unless the columns are individually labelled.
        return [(row[0], int(row[1])) for row in result.all()]

    async def count_products(
        self, category_id: uuid.UUID | str, *, active_only: bool
    ) -> int:
        """How many products reference this category.

        ``active_only`` is required rather than defaulted because the two
        callers want genuinely different things: a public listing must count
        only what a shopper can actually buy, while a hard delete has to count
        *every* reference, since ``products.category_id`` is ``ON DELETE
        RESTRICT`` and an inactive product still blocks the delete.
        """
        query = select(func.count()).select_from(Product).where(
            Product.category_id == self._as_uuid(category_id)
        )
        if active_only:
            query = query.where(Product.is_active.is_(True))
        result = await self.session.execute(query)
        return int(result.scalar_one())

    async def update(self, category: Category, **values: object) -> Category:
        for key, value in values.items():
            setattr(category, key, value)
        await self.session.flush()
        # ``updated_at`` uses ``onupdate=func.now()``: the database computes it,
        # so the instance is expired after flushing and must be reloaded before
        # the response is serialised.
        await self.session.refresh(category)
        return category

    async def delete(self, category: Category) -> None:
        await self.session.delete(category)
        await self.session.flush()

    @staticmethod
    def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

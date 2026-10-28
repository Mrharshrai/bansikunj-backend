"""Category business logic.

Public callers only ever see active categories. Slug and name uniqueness is
resolved here (not by the database exception) so a collision comes back as a
clean 409 instead of a 500.
"""
from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import ConflictError, NotFoundError, ValidationError
from ..core.logging import get_logger
from ..core.slugs import slugify, with_suffix
from ..models.category import Category
from ..repositories.category_repository import CategoryRepository
from ..schemas.category import CategoryCreate, CategoryUpdate

logger = get_logger(__name__)

_MAX_SLUG_ATTEMPTS = 50


class CategoryService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.categories = CategoryRepository(session)

    # ------------------------------------------------------------------ read
    async def list_categories(
        self, *, include_inactive: bool = False
    ) -> list[tuple[Category, int]]:
        """Active categories in display order, each with its product count."""
        return await self.categories.list_all(include_inactive=include_inactive)

    async def get_by_slug(self, slug: str) -> Category:
        category = await self.categories.get_by_slug(slug)
        if category is None:
            raise NotFoundError("Category not found", error_code="category_not_found")
        return category

    async def get_by_id(self, category_id: uuid.UUID | str) -> Category:
        category = await self.categories.get_by_id(category_id)
        if category is None:
            raise NotFoundError("Category not found", error_code="category_not_found")
        return category

    async def product_count(self, category: Category) -> int:
        return await self.categories.count_products(category.id, active_only=True)

    async def resolve(self, reference: uuid.UUID | str | Category) -> Category:
        """Look up a category by id, slug, or pass an already-loaded row through.

        Used when creating a product, so an admin can reference a category
        either way and a missing one fails with 404 rather than a FK violation.
        """
        if isinstance(reference, Category):
            return reference
        if isinstance(reference, uuid.UUID):
            return await self.get_by_id(reference)
        try:
            return await self.get_by_id(str(reference))
        except ValueError:
            pass
        category = await self.categories.get_by_slug(str(reference), include_inactive=True)
        if category is None:
            raise NotFoundError("Category not found", error_code="category_not_found")
        return category

    # ----------------------------------------------------------------- write
    async def create(self, payload: CategoryCreate) -> Category:
        values = payload.model_dump()
        if await self.categories.name_exists(payload.name):
            raise ConflictError(
                "A category with this name already exists",
                error_code="category_name_taken",
            )

        requested = values.pop("slug", None)
        slug = await self._unique_slug(
            requested or slugify(payload.name) or f"category-{uuid.uuid4().hex[:8]}",
            exclude_id=None,
        )
        category = await self.categories.create(slug=slug, **values)
        logger.info("category.created", category_id=str(category.id), slug=category.slug)
        return category

    async def update(
        self, category_id: uuid.UUID | str, payload: CategoryUpdate
    ) -> Category:
        category = await self.get_by_id(category_id)
        changes = payload.model_dump(exclude_unset=True)

        new_name = changes.get("name")
        if new_name is not None and new_name.lower() != category.name.lower():
            if await self.categories.name_exists(new_name, exclude_id=category.id):
                raise ConflictError(
                    "A category with this name already exists",
                    error_code="category_name_taken",
                )

        if "slug" in changes:
            requested = changes.pop("slug")
            if requested and requested != category.slug:
                changes["slug"] = await self._unique_slug(
                    requested, exclude_id=category.id
                )

        if changes:
            category = await self.categories.update(category, **changes)
        logger.info(
            "category.updated",
            category_id=str(category.id),
            fields=sorted(changes),
        )
        return category

    async def deactivate(
        self, category_id: uuid.UUID | str, *, force: bool = False
    ) -> Category:
        """Hide a category.

        Refuses to hide a category that still has active products, because those
        products would disappear from every listing with no explanation. Pass
        ``force`` to hide it anyway.
        """
        category = await self.get_by_id(category_id)
        active_products = await self.categories.count_products(
            category.id, active_only=True
        )
        if active_products and not force:
            raise ConflictError(
                "This category still has products; deactivate or move them first",
                error_code="category_not_empty",
                details={"product_count": active_products},
            )
        category.is_active = False
        await self.session.flush()
        # ``updated_at`` is a server-side ``onupdate`` default; reload it so the
        # response does not trigger a lazy load in an async context.
        await self.session.refresh(category)
        logger.info(
            "category.deactivated",
            category_id=str(category.id),
            slug=category.slug,
            forced=force,
        )
        return category

    async def delete(self, category_id: uuid.UUID | str) -> None:
        """Hard delete. Only possible when no product references the category."""
        category = await self.get_by_id(category_id)
        # Every reference blocks the delete, not just the active ones: the
        # foreign key is ON DELETE RESTRICT, so an inactive product would
        # otherwise surface as a raw IntegrityError instead of this 409.
        count = await self.categories.count_products(category.id, active_only=False)
        if count:
            raise ConflictError(
                "This category still has products and cannot be deleted",
                error_code="category_not_empty",
                details={"product_count": count},
            )
        await self.categories.delete(category)
        logger.info("category.deleted", category_id=str(category.id), slug=category.slug)

    # --------------------------------------------------------------- internal
    async def _unique_slug(
        self, base: str, *, exclude_id: uuid.UUID | None
    ) -> str:
        """Return ``base`` or the first free ``base-2``, ``base-3``..."""
        candidate = base
        for attempt in range(1, _MAX_SLUG_ATTEMPTS + 1):
            if not await self.categories.slug_exists(candidate, exclude_id=exclude_id):
                return candidate
            candidate = with_suffix(base, attempt + 1)
        raise ValidationError(
            "Could not derive a unique slug; supply one explicitly",
            error_code="slug_exhausted",
        )


__all__ = ["CategoryService"]

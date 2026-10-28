"""Product persistence: filtering, sorting, and pagination.

Every listing query is assembled from a fixed set of clauses driven by the
:class:`ProductFilter` dataclass. The sort option is mapped through an explicit
lookup table rather than interpolated, so a client-supplied ``sort`` can never
reach the SQL text.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models.category import Category
from ..models.product import Product

#: Whitelist of ``sort`` query values to ORDER BY clauses. The first clause is
#: the primary key; the rest are tie-breakers so paging is deterministic.
_SORT_CLAUSES: dict[str, tuple] = {
    "popular": (Product.review_count.desc(), Product.is_best_seller.desc(), Product.created_at.desc()),
    "price-asc": (Product.price.asc(), Product.created_at.desc()),
    "price-desc": (Product.price.desc(), Product.created_at.desc()),
    "rating": (Product.rating.desc().nullslast(), Product.review_count.desc(), Product.created_at.desc()),
    "newest": (Product.created_at.desc(),),
}

#: Escapes LIKE metacharacters so a search for "50%" is a literal search.
_LIKE_ESCAPE = "\\"


def like_pattern(term: str) -> str:
    escaped = (
        term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


@dataclass(frozen=True, slots=True)
class ProductFilter:
    """Everything that narrows a product listing."""

    q: str | None = None
    category_slug: str | None = None
    tag: str | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    is_best_seller: bool | None = None
    in_stock: bool | None = None
    include_inactive: bool = False


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ create
    async def create(self, **values: object) -> Product:
        product = Product(**values)
        self.session.add(product)
        await self.session.flush()
        return product

    # -------------------------------------------------------------------- read
    async def get_by_id(self, product_id: uuid.UUID | str) -> Product | None:
        result = await self.session.execute(
            select(Product)
            .options(selectinload(Product.category))
            .where(Product.id == self._as_uuid(product_id))
        )
        return result.scalar_one_or_none()

    async def get_by_slug(
        self, slug: str, *, include_inactive: bool = False
    ) -> Product | None:
        result = await self.session.execute(
            select(Product)
            .options(selectinload(Product.category))
            .where(
                Product.slug == slug.strip().lower(),
                *([] if include_inactive else (Product.is_active.is_(True),)),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id_or_slug(
        self, reference: str | uuid.UUID, *, include_inactive: bool = False
    ) -> Product | None:
        """Resolve a path parameter that may be either a UUID or a slug."""
        try:
            return await self.get_by_id(reference)
        except (ValueError, TypeError):
            return await self.get_by_slug(str(reference), include_inactive=include_inactive)

    async def get_locked_by_id_or_slug(
        self, reference: str | uuid.UUID, *, include_inactive: bool = False
    ) -> Product | None:
        """Resolve a reference and hold the row lock until the transaction ends.

        A stock adjustment is a read-modify-write, so it must queue behind
        ``OrderRepository``'s locked decrement of the same row. Reading without
        the lock would let a concurrent order's decrement be silently
        overwritten by an adjustment computed from the pre-order quantity.

        ``selectinload`` issues its own query, so ``FOR UPDATE`` applies to the
        product table alone and does not lock the category row.
        """
        try:
            identifier = self._as_uuid(reference)
            by_id = True
        except (ValueError, TypeError):
            identifier = str(reference).strip().lower()
            by_id = False

        column = Product.id if by_id else Product.slug
        result = await self.session.execute(
            select(Product)
            .options(selectinload(Product.category))
            .where(
                column == identifier,
                *([] if include_inactive else (Product.is_active.is_(True),)),
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        filters: ProductFilter | None = None,
        sort: str = "popular",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Product], int]:
        """Return one page of products plus the total number of matches."""
        filters = filters or ProductFilter()
        conditions = self._conditions(filters)

        total_result = await self.session.execute(
            select(func.count()).select_from(Product).where(*conditions)
        )
        total = int(total_result.scalar_one())

        order_by = _SORT_CLAUSES.get(sort, _SORT_CLAUSES["popular"])
        result = await self.session.execute(
            select(Product)
            .options(selectinload(Product.category))
            .where(*conditions)
            .order_by(*order_by)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().unique().all()), total

    async def distinct_tags(self, *, limit: int = 100) -> list[str]:
        """Every tag in use by an active product, for building filter chips."""
        rows = await self.session.execute(
            select(func.unnest(Product.tags).label("tag"))
            .where(Product.is_active.is_(True))
            .group_by("tag")
            .order_by("tag")
            .limit(limit)
        )
        return [row[0] for row in rows.all()]

    async def slug_exists(self, slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        query = select(Product.id).where(Product.slug == slug)
        if exclude_id is not None:
            query = query.where(Product.id != exclude_id)
        return (await self.session.execute(query.limit(1))).first() is not None

    async def count_in_category(self, category_id: uuid.UUID | str) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(Product)
            .where(Product.category_id == self._as_uuid(category_id))
        )
        return int(result.scalar_one())

    async def stock_counts(self, *, low_stock_threshold: int) -> dict[str, int]:
        """Catalogue-wide stock tallies in one query.

        Kept out of :meth:`list` because these totals describe every product,
        not the current page, so they must not shift when an admin pages or
        filters the report.

        ``low_stock_threshold`` is passed in rather than using the module
        default, so a caller asking for a custom threshold gets a tally that
        agrees with the ``is_low_stock`` flags in the same response instead of a
        header that silently contradicts the rows.
        """
        result = await self.session.execute(
            select(
                func.count(Product.id),
                func.count(Product.id).filter(Product.stock_quantity == 0),
                func.count(Product.id).filter(
                    Product.stock_quantity.between(1, low_stock_threshold)
                ),
            ).where(Product.is_active.is_(True))
        )
        total, out_of_stock, low = result.one()
        return {
            "total": int(total),
            "out_of_stock": int(out_of_stock),
            "low_stock": int(low),
            "in_stock": int(total) - int(out_of_stock),
        }

    async def list_for_inventory(
        self,
        *,
        low_stock_threshold: int,
        only_low_stock: bool = False,
        only_out_of_stock: bool = False,
        include_inactive: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Product], int]:
        """Products ordered by how urgently they need restocking.

        Scarcest first (quantity ascending, zero included) because that is the
        order a restocking decision is made in. ``id`` breaks ties so paging is
        stable when several products share a quantity.
        """
        conditions: list = []
        if not include_inactive:
            conditions.append(Product.is_active.is_(True))
        if only_out_of_stock:
            conditions.append(Product.stock_quantity == 0)
        elif only_low_stock:
            conditions.append(
                Product.stock_quantity.between(1, low_stock_threshold)
            )

        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(Product).where(*conditions)
                )
            ).scalar_one()
        )
        result = await self.session.execute(
            select(Product)
            .options(selectinload(Product.category))
            .where(*conditions)
            .order_by(Product.stock_quantity.asc(), Product.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().unique().all()), total

    # ------------------------------------------------------------------ write
    async def update(self, product: Product, **values: object) -> Product:
        for key, value in values.items():
            setattr(product, key, value)
        await self.session.flush()
        if "category_id" in values:
            # ``populate_existing`` is essential here. The product is already
            # persistent, so a plain re-select hands back the same
            # identity-mapped instance and SQLAlchemy leaves the ``category``
            # relationship it loaded earlier in the request untouched. Without
            # it the response reports the category the product just left.
            result = await self.session.execute(
                select(Product)
                .options(selectinload(Product.category))
                .where(Product.id == product.id)
                .execution_options(populate_existing=True)
            )
            refreshed = result.scalar_one_or_none()
            if refreshed is not None:
                return refreshed
        return product

    async def deactivate(self, product: Product) -> Product:
        """Soft delete.

        Catalogue rows are referenced by orders, so removal is a flag flip; the
        public listing filters on ``is_active`` and the row keeps its history.
        """
        product.is_active = False
        await self.session.flush()
        return product

    # --------------------------------------------------------------- internal
    def _conditions(self, filters: ProductFilter) -> tuple:
        conditions: list = []
        if not filters.include_inactive:
            conditions.append(Product.is_active.is_(True))

        if filters.q:
            pattern = like_pattern(filters.q.strip())
            # array_to_string lets a substring search match a single tag; the
            # GIN index on tags still serves the exact-match tag filter below.
            conditions.append(
                or_(
                    Product.name.ilike(pattern, escape=_LIKE_ESCAPE),
                    Product.description.ilike(pattern, escape=_LIKE_ESCAPE),
                    func.array_to_string(Product.tags, " ").ilike(
                        pattern, escape=_LIKE_ESCAPE
                    ),
                )
            )
        if filters.category_slug:
            conditions.append(
                Product.category_id.in_(
                    select(Category.id).where(
                        Category.slug == filters.category_slug.strip().lower()
                    )
                )
            )
        if filters.tag:
            conditions.append(Product.tags.contains([filters.tag.strip().lower()]))
        if filters.min_price is not None:
            conditions.append(Product.price >= filters.min_price)
        if filters.max_price is not None:
            conditions.append(Product.price <= filters.max_price)
        if filters.is_best_seller is not None:
            conditions.append(Product.is_best_seller.is_(filters.is_best_seller))
        if filters.in_stock is not None:
            conditions.append(
                Product.stock_quantity > 0
                if filters.in_stock
                else Product.stock_quantity == 0
            )
        return tuple(conditions)

    @staticmethod
    def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))

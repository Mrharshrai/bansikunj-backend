"""Product catalogue business logic.

The service owns every catalogue rule: which rows a public caller may see, how a
slug is derived, and the cross-field constraints (``mrp`` vs ``price``, category
existence) that cannot be expressed in a Pydantic model because they depend on
the stored row.
"""
from __future__ import annotations

import math
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from ..core.exceptions import ConflictError, NotFoundError, ValidationError
from ..core.logging import get_logger
from ..core.slugs import slugify, with_suffix
from ..models.product import Product
from ..repositories.product_repository import ProductFilter, ProductRepository
from ..schemas.product import ProductCreate, ProductUpdate, StockAdjustment
from .category_service import CategoryService

logger = get_logger(__name__)

_MAX_SLUG_ATTEMPTS = 50


class ProductService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.products = ProductRepository(session)
        self.categories = CategoryService(session)

    # ------------------------------------------------------------------ read
    async def list_products(
        self,
        *,
        filters: ProductFilter,
        sort: str,
        page: int,
        limit: int,
    ) -> tuple[list[Product], int]:
        """One page of products plus the total number of matches."""
        offset = (page - 1) * limit
        return await self.products.list(
            filters=filters, sort=sort, limit=limit, offset=offset
        )

    async def get(
        self, reference: str | uuid.UUID, *, include_inactive: bool = False
    ) -> Product:
        """Fetch a product by id or slug.

        An inactive product is a 404 for the public, so a delisted item cannot be
        discovered by guessing its id.
        """
        product = await self.products.get_by_id_or_slug(
            reference, include_inactive=include_inactive
        )
        if product is None or (not include_inactive and not product.is_active):
            raise NotFoundError("Product not found", error_code="product_not_found")
        return product

    async def tags(self) -> list[str]:
        return await self.products.distinct_tags()

    # ----------------------------------------------------------------- write
    async def create(self, payload: ProductCreate) -> Product:
        values = payload.model_dump()
        category_slug = values.pop("category")
        requested_slug = values.pop("slug", None)

        category = await self.categories.resolve(category_slug)
        if not category.is_active:
            raise ValidationError(
                "Products cannot be added to an inactive category",
                error_code="category_inactive",
            )

        slug = await self._unique_slug(
            requested_slug or slugify(payload.name) or f"product-{uuid.uuid4().hex[:8]}",
            exclude_id=None,
        )
        product = await self.products.create(
            category_id=category.id,
            slug=slug,
            rating=None,
            review_count=0,
            **values,
        )
        logger.info(
            "product.created",
            product_id=str(product.id),
            slug=product.slug,
            category_slug=category.slug,
        )
        return await self.products.get_by_id(product.id)  # type: ignore[return-value]

    async def update(
        self, product_id: uuid.UUID | str, payload: ProductUpdate
    ) -> Product:
        product = await self.get(product_id, include_inactive=True)
        changes = payload.model_dump(exclude_unset=True)

        if "category" in changes:
            category_ref = changes.pop("category")
            if category_ref is not None:
                category = await self.categories.resolve(category_ref)
                if not category.is_active:
                    raise ValidationError(
                        "Products cannot be moved into an inactive category",
                        error_code="category_inactive",
                    )
                changes["category_id"] = category.id

        if "slug" in changes:
            requested = changes.pop("slug")
            if requested and requested != product.slug:
                changes["slug"] = await self._unique_slug(
                    requested, exclude_id=product.id
                )

        self._validate_price_pair(product, changes)

        if changes:
            product = await self.products.update(product, **changes)
        logger.info("product.updated", product_id=str(product.id), fields=sorted(changes))
        return product

    async def replace(
        self, product_id: uuid.UUID | str, payload: ProductCreate
    ) -> Product:
        """Full replacement (PUT): every mutable field is taken from the body."""
        current = await self.get(product_id, include_inactive=True)
        full = payload.model_dump(exclude_unset=False)
        for field in ("id", "slug"):
            full.pop(field, None)
        # Fields absent from the body reset to their defaults, so drive the
        # partial updater with an explicit "set" of every replaceable field.
        return await self.update(current.id, ProductUpdate(**full))

    async def deactivate(self, product_id: uuid.UUID | str) -> Product:
        product = await self.get(product_id, include_inactive=True)
        if not product.is_active:
            return product
        product = await self.products.deactivate(product)
        logger.info("product.deactivated", product_id=str(product.id), slug=product.slug)
        return product

    # ------------------------------------------------------------------ stock
    async def adjust_stock(
        self, product_id: uuid.UUID | str, payload: StockAdjustment
    ) -> tuple[Product, int, int]:
        """Change only ``stock_quantity`` and return ``(product, previous, new)``.

        The row is re-read under ``FOR UPDATE`` so this queues behind a
        concurrent order's decrement instead of racing it. Writing a stale
        absolute value would otherwise undo stock that a real order just sold.
        """
        product = await self.products.get_locked_by_id_or_slug(
            product_id, include_inactive=True
        )
        if product is None:
            raise NotFoundError("Product not found", error_code="product_not_found")

        previous = product.stock_quantity
        target = payload.quantity if payload.is_absolute else previous + (payload.delta or 0)

        if target < 0:
            # The column carries a CHECK (stock_quantity >= 0), so letting this
            # through would surface as a 500 IntegrityError. A shortage is a
            # conflict with current state, not a malformed request.
            raise ConflictError(
                f"Cannot remove {abs(target)} units: only {previous} in stock",
                error_code="insufficient_stock",
                details={
                    "stock_quantity": previous,
                    "requested": target,
                    "shortfall": abs(target),
                },
            )

        product = await self.products.update(product, stock_quantity=target)
        logger.info(
            "product.stock_adjusted",
            product_id=str(product.id),
            slug=product.slug,
            previous_quantity=previous,
            new_quantity=target,
            delta=target - previous,
            reason=payload.reason,
        )
        return product, previous, target

    async def inventory(
        self,
        *,
        low_stock_threshold: int,
        only_low_stock: bool,
        only_out_of_stock: bool,
        include_inactive: bool,
        page: int,
        limit: int,
    ) -> tuple[list[Product], int, dict[str, int]]:
        """One page of the stock report, its total, and the catalogue tallies."""
        offset = (page - 1) * limit
        products, total = await self.products.list_for_inventory(
            low_stock_threshold=low_stock_threshold,
            only_low_stock=only_low_stock,
            only_out_of_stock=only_out_of_stock,
            include_inactive=include_inactive,
            limit=limit,
            offset=offset,
        )
        return products, total, await self.products.stock_counts(
            low_stock_threshold=low_stock_threshold
        )

    # --------------------------------------------------------------- internal
    def _validate_price_pair(
        self, product: Product, changes: dict[str, object]
    ) -> None:
        """Reject an update that would leave ``mrp`` below ``price``.

        The incoming value and the stored value are combined first, so lowering
        ``price`` below an existing ``mrp`` is caught as well as sending a bad
        ``mrp`` explicitly.
        """
        if "price" not in changes and "mrp" not in changes:
            return
        new_price = changes.get("price", product.price)
        new_mrp = changes.get("mrp", product.mrp)
        if new_price is None or new_mrp is None:
            return
        price = Decimal(str(new_price))
        mrp = Decimal(str(new_mrp))
        if mrp < price:
            raise ValidationError(
                "mrp must be greater than or equal to price",
                error_code="mrp_below_price",
                details={"price": str(price), "mrp": str(mrp)},
            )

    async def _unique_slug(self, base: str, *, exclude_id: uuid.UUID | None) -> str:
        candidate = base
        for attempt in range(1, _MAX_SLUG_ATTEMPTS + 1):
            if not await self.products.slug_exists(candidate, exclude_id=exclude_id):
                return candidate
            candidate = with_suffix(base, attempt + 1)
        raise ValidationError(
            "Could not derive a unique slug; supply one explicitly",
            error_code="slug_exhausted",
        )

    @staticmethod
    def page_count(total: int, limit: int) -> int:
        return max(1, math.ceil(total / limit)) if total else 0


__all__ = ["ProductService"]

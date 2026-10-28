"""Product endpoints.

Route order matters: ``/products/tags`` is registered *before*
``/products/{reference}`` so the literal path is not swallowed by the
parameterised route (a product slug could legitimately be ``tags``).
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from pydantic import BaseModel

from ...core.dependencies import DbSessionDep, require_admin
from ...core.envelope import SuccessEnvelope
from ...core.exceptions import ValidationError
from ...models.user import User
from ...repositories.product_repository import ProductFilter
from ...schemas.product import (
    InventoryItem,
    InventoryPage,
    ProductCreate,
    ProductOut,
    ProductPage,
    ProductUpdate,
    SortOption,
    StockAdjustment,
)
from ...services.product_service import ProductService

router = APIRouter(prefix="/products", tags=["products"])


def get_product_service(session: DbSessionDep) -> ProductService:
    return ProductService(session)


ProductServiceDep = Annotated[ProductService, Depends(get_product_service)]
AdminDep = Annotated[User, Depends(require_admin)]


class SingleProduct(BaseModel):
    product: ProductOut


class StockAdjustmentResult(BaseModel):
    product: ProductOut
    previous_quantity: int
    new_quantity: int
    delta: int
    reason: str | None = None


class TagList(BaseModel):
    tags: list[str]
    total: int


def _money(value: str | None, field: str) -> Decimal | None:
    """Parse an optional price filter, reporting a clean 422 for junk input."""
    if value is None or value == "":
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(
            f"{field} must be a decimal number",
            error_code="validation_error",
            details={"field": field, "value": value},
        ) from exc


# NOTE: /tags must be declared before /{reference}.
@router.get("/tags", response_model=SuccessEnvelope[TagList], summary="List all tags")
async def list_tags(service: ProductServiceDep) -> SuccessEnvelope[TagList]:
    """Every tag used by an active product, for building filter controls."""
    tags = await service.tags()
    return SuccessEnvelope[TagList](
        message="Tags retrieved successfully", data=TagList(tags=tags, total=len(tags))
    )


# NOTE: /inventory must be declared before /{reference} for the same reason.
@router.get(
    "/inventory",
    response_model=SuccessEnvelope[InventoryPage],
    summary="Stock report (admin)",
)
async def list_inventory(
    service: ProductServiceDep,
    _: AdminDep,
    low_stock_threshold: Annotated[
        int, Query(ge=0, le=10_000_000, description="Units at or below which stock is 'low'.")
    ] = 10,
    low_stock: Annotated[
        bool, Query(description="Only rows in the low-stock band (1..threshold).")
    ] = False,
    out_of_stock: Annotated[
        bool, Query(description="Only rows at zero units.")
    ] = False,
    include_inactive: Annotated[
        bool, Query(description="Include delisted products.")
    ] = False,
    page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page.")] = 20,
) -> SuccessEnvelope[InventoryPage]:
    """Products with their real quantities, scarcest first.

    Unlike the public listing, this exposes ``stock_quantity`` instead of just
    ``in_stock``, because deciding what to reorder needs the number. The
    ``out_of_stock`` and ``low_stock`` tallies cover the whole active catalogue
    rather than the current page.
    """
    if low_stock and out_of_stock:
        raise ValidationError(
            "low_stock and out_of_stock are mutually exclusive",
            error_code="validation_error",
        )

    products, total, counts = await service.inventory(
        low_stock_threshold=low_stock_threshold,
        only_low_stock=low_stock,
        only_out_of_stock=out_of_stock,
        include_inactive=include_inactive,
        page=page,
        limit=limit,
    )
    return SuccessEnvelope[InventoryPage](
        message="Stock report retrieved successfully",
        data=InventoryPage(
            products=[
                InventoryItem(
                    id=product.id,
                    name=product.name,
                    slug=product.slug,
                    category=product.category.slug if product.category else None,
                    image_url=product.image_url,
                    price=product.price,
                    stock_quantity=product.stock_quantity,
                    in_stock=product.in_stock,
                    is_active=product.is_active,
                    is_low_stock=0 < product.stock_quantity <= low_stock_threshold,
                )
                for product in products
            ],
            total=total,
            page=page,
            limit=limit,
            pages=ProductService.page_count(total, limit),
            low_stock_threshold=low_stock_threshold,
            out_of_stock=counts["out_of_stock"],
            low_stock=counts["low_stock"],
            in_stock=counts["in_stock"],
        ),
    )


@router.get(
    "", response_model=SuccessEnvelope[ProductPage], summary="Browse the catalogue"
)
async def list_products(
    service: ProductServiceDep,
    q: Annotated[str | None, Query(description="Substring match on name, description or tag.")] = None,
    category: Annotated[str | None, Query(description="Category slug.")] = None,
    tag: Annotated[str | None, Query(description="Exact tag match.")] = None,
    min_price: Annotated[str | None, Query(description="Inclusive lower price bound.")] = None,
    max_price: Annotated[str | None, Query(description="Inclusive upper price bound.")] = None,
    is_best_seller: Annotated[bool | None, Query(description="Only best sellers.")] = None,
    in_stock: Annotated[bool | None, Query(description="Filter by availability.")] = None,
    sort: Annotated[SortOption, Query(description="Result ordering.")] = "popular",
    page: Annotated[int, Query(ge=1, description="1-based page number.")] = 1,
    limit: Annotated[int, Query(ge=1, le=100, description="Items per page.")] = 20,
) -> SuccessEnvelope[ProductPage]:
    """Paginated, filtered, sorted product listing.

    Only active products are returned. The total reflects the filters, not the
    page, so a client can compute the number of pages.
    """
    low = _money(min_price, "min_price")
    high = _money(max_price, "max_price")
    if low is not None and high is not None and low > high:
        raise ValidationError(
            "min_price cannot be greater than max_price",
            error_code="validation_error",
            details={"min_price": str(low), "max_price": str(high)},
        )

    filters = ProductFilter(
        q=(q or "").strip() or None,
        category_slug=(category or "").strip() or None,
        tag=(tag or "").strip() or None,
        min_price=low,
        max_price=high,
        is_best_seller=is_best_seller,
        in_stock=in_stock,
    )
    products, total = await service.list_products(
        filters=filters, sort=sort, page=page, limit=limit
    )
    return SuccessEnvelope[ProductPage](
        message="Products retrieved successfully",
        data=ProductPage(
            products=[ProductOut.from_entity(p) for p in products],
            total=total,
            page=page,
            limit=limit,
            pages=service.page_count(total, limit),
            sort=sort,
            filters={
                k: v
                for k, v in {
                    "q": filters.q,
                    "category": filters.category_slug,
                    "tag": filters.tag,
                    "min_price": str(low) if low is not None else None,
                    "max_price": str(high) if high is not None else None,
                    "is_best_seller": filters.is_best_seller,
                    "in_stock": filters.in_stock,
                }.items()
                if v is not None
            },
        ),
    )


@router.get(
    "/{reference}",
    response_model=SuccessEnvelope[SingleProduct],
    summary="Fetch one product",
)
async def get_product(
    reference: Annotated[
        str,
        Path(description="Product UUID or slug, e.g. 'belgian-dark-chunk'."),
    ],
    service: ProductServiceDep,
) -> SuccessEnvelope[SingleProduct]:
    """Fetch a product by id or slug.

    An inactive product returns 404, so a delisted item cannot be discovered by
    guessing its id.
    """
    product = await service.get(reference)
    return SuccessEnvelope[SingleProduct](
        message="Product retrieved",
        data=SingleProduct(product=ProductOut.from_entity(product)),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessEnvelope[SingleProduct],
    summary="Create a product",
)
async def create_product(
    payload: ProductCreate, service: ProductServiceDep, _: AdminDep
) -> SuccessEnvelope[SingleProduct]:
    """Create a product. ``category`` is a slug; ``slug`` is derived if omitted."""
    product = await service.create(payload)
    return SuccessEnvelope[SingleProduct](
        message="Product created successfully",
        data=SingleProduct(product=ProductOut.from_entity(product)),
    )


@router.put(
    "/{reference}",
    response_model=SuccessEnvelope[SingleProduct],
    summary="Replace a product",
)
async def replace_product(
    reference: str, payload: ProductCreate, service: ProductServiceDep, _: AdminDep
) -> SuccessEnvelope[SingleProduct]:
    """Full replacement: every field is taken from the body, defaults included."""
    product = await service.replace(reference, payload)
    return SuccessEnvelope[SingleProduct](
        message="Product updated successfully",
        data=SingleProduct(product=ProductOut.from_entity(product)),
    )


@router.patch(
    "/{reference}",
    response_model=SuccessEnvelope[SingleProduct],
    summary="Partially update a product",
)
async def patch_product(
    reference: str, payload: ProductUpdate, service: ProductServiceDep, _: AdminDep
) -> SuccessEnvelope[SingleProduct]:
    """Only fields present in the body are changed."""
    product = await service.update(reference, payload)
    return SuccessEnvelope[SingleProduct](
        message="Product updated successfully",
        data=SingleProduct(product=ProductOut.from_entity(product)),
    )


@router.patch(
    "/{reference}/stock",
    response_model=SuccessEnvelope[StockAdjustmentResult],
    summary="Adjust stock (admin)",
)
async def adjust_product_stock(
    reference: str, payload: StockAdjustment, service: ProductServiceDep, _: AdminDep
) -> SuccessEnvelope[StockAdjustmentResult]:
    """Change a product's quantity without touching any other field.

    Send ``delta`` for a relative movement (``{"delta": 50}`` for a delivery,
    ``{"delta": -2}`` for damage) or ``quantity`` to set an absolute count. A
    delta that would push stock below zero is a 409, not a silent clamp.

    Unlike ``PATCH /products/{reference}``, this cannot overwrite a concurrent
    edit to the price or description, which is what made editing stock through
    the full product form unsafe.
    """
    product, previous, new = await service.adjust_stock(reference, payload)
    return SuccessEnvelope[StockAdjustmentResult](
        message="Stock updated successfully",
        data=StockAdjustmentResult(
            product=ProductOut.from_entity(product),
            previous_quantity=previous,
            new_quantity=new,
            delta=new - previous,
            reason=payload.reason,
        ),
    )


@router.delete(
    "/{reference}",
    response_model=SuccessEnvelope[None],
    summary="Delist a product",
)
async def delete_product(
    reference: str, service: ProductServiceDep, _: AdminDep
) -> SuccessEnvelope[None]:
    """Delist a product (soft delete).

    The row is kept so historical orders keep referring to a real product; it
    disappears from every public listing immediately.
    """
    await service.deactivate(reference)
    return SuccessEnvelope[None](message="Product delisted successfully", data=None)


__all__ = ["router"]

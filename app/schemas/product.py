"""Product request/response schemas."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .category import normalise_slug

SortOption = Literal["popular", "price-asc", "price-desc", "rating", "newest"]

MAX_TAGS = 20
MAX_TAG_LENGTH = 50


def _clean_tags(value: list[str] | None) -> list[str]:
    """Lowercase, trim, de-duplicate, and cap the tag list."""
    if not value:
        return []
    seen: dict[str, None] = {}
    for raw in value:
        tag = str(raw).strip().lower()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"Each tag must be {MAX_TAG_LENGTH} characters or fewer")
        seen.setdefault(tag, None)
        if len(seen) > MAX_TAGS:
            raise ValueError(f"A product may have at most {MAX_TAGS} tags")
    return list(seen)


class _ProductValidators:
    """Validators shared by the create and update schemas.

    All of them tolerate ``None``: the update schema uses the same mixin, where
    every field is optional and an absent field means "leave unchanged".
    """

    @field_validator("image_url")
    @classmethod
    def _image(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not (value.startswith("http://") or value.startswith("https://") or value.startswith("/")):
            raise ValueError(
                "image_url must be an http(s) URL or a root-relative path, e.g. '/assets/cake.png'"
            )
        return value

    @field_validator("tags")
    @classmethod
    def _tags(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_tags(value)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Product name cannot be blank")
        return value

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Product description cannot be blank")
        return value

    @field_validator("price")
    @classmethod
    def _price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("price must be greater than 0")
        return value

    @field_validator("mrp")
    @classmethod
    def _mrp(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("mrp must be greater than 0 when supplied")
        return value

    @field_validator("stock_quantity")
    @classmethod
    def _stock(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("stock_quantity cannot be negative")
        return value

    @field_validator("category")
    @classmethod
    def _category(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip().lower()

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        return normalise_slug(value)


class ProductBase(_ProductValidators, BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=200)
    description: str = Field(min_length=1, max_length=5000)
    #: Category slug, e.g. ``"soft-chewy"``. Resolved server-side.
    category: str = Field(min_length=1, max_length=150)
    #: Derived from ``name`` by the service when omitted.
    slug: str | None = Field(default=None, max_length=200)
    price: Decimal = Field(max_digits=12, decimal_places=2)
    mrp: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    stock_quantity: int = Field(default=0, ge=0, le=10_000_000)
    image_url: str = Field(max_length=500)
    weight: str | None = Field(default=None, max_length=80)
    tags: list[str] = Field(default_factory=list)
    is_active: bool = True
    is_best_seller: bool = False

    @model_validator(mode="after")
    def _mrp_not_below_price(self) -> "ProductBase":
        if self.mrp is not None and self.mrp < self.price:
            raise ValueError("mrp must be greater than or equal to price")
        return self


class ProductCreate(ProductBase):
    """Creating a product."""


class ProductUpdate(_ProductValidators, BaseModel):
    """Partial update; only the fields present in the body are changed.

    Cross-field rules that depend on the stored row (``mrp`` vs the current
    ``price``) are enforced in the service, which is the only place that can see
    both.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, min_length=1, max_length=5000)
    category: str | None = Field(default=None, min_length=1, max_length=150)
    slug: str | None = Field(default=None, max_length=200)
    price: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    mrp: Decimal | None = Field(default=None, max_digits=12, decimal_places=2)
    stock_quantity: int | None = Field(default=None, ge=0, le=10_000_000)
    image_url: str | None = Field(default=None, max_length=500)
    weight: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = None
    is_active: bool | None = None
    is_best_seller: bool | None = None


class ProductOut(BaseModel):
    """A product as returned by the API.

    Field names deliberately match the Angular ``Product`` interface
    (``category``, ``category_name``, ``image_url``, ``in_stock``,
    ``is_best_seller``, ``review_count``) so the frontend needs only an envelope
    adapter.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    description: str
    weight: str | None = None

    price: Decimal
    mrp: Decimal | None = None
    discount_percent: int | None = None
    in_stock: bool = False
    stock_quantity: int = 0

    category_id: uuid.UUID
    category: str | None = None
    category_name: str | None = None
    category_slug: str | None = None

    image_url: str
    tags: list[str] = Field(default_factory=list)

    is_active: bool
    is_best_seller: bool
    rating: Decimal | None = None
    review_count: int = 0

    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, product) -> "ProductOut":
        """Build the response body from an ORM row.

        The ``category`` relationship must be loaded; the repositories select it
        with ``selectinload`` so this is never a lazy load per row.
        """
        category = getattr(product, "category", None)
        return cls(
            id=product.id,
            name=product.name,
            slug=product.slug,
            description=product.description,
            weight=product.weight,
            price=product.price,
            mrp=product.mrp,
            discount_percent=product.discount_percent,
            in_stock=product.in_stock,
            stock_quantity=product.stock_quantity,
            category_id=product.category_id,
            category=category.slug if category else None,
            category_name=category.name if category else None,
            category_slug=category.slug if category else None,
            image_url=product.image_url,
            tags=list(product.tags or []),
            is_active=product.is_active,
            is_best_seller=product.is_best_seller,
            rating=product.rating,
            review_count=product.review_count,
            created_at=product.created_at,
            updated_at=product.updated_at,
        )


class ProductPage(BaseModel):
    """Envelope ``data`` for a paginated product listing."""

    products: list[ProductOut]
    total: int
    page: int
    limit: int
    pages: int
    sort: str
    filters: dict[str, object] = Field(default_factory=dict)


class StockAdjustment(BaseModel):
    """Body for a stock-only change.

    Two mutually exclusive modes, so an admin can express either intent without
    the caller having to know the current quantity:

    * ``delta`` -- relative movement, e.g. ``-3`` for shrinkage or a return, or
      ``+50`` for a delivery. Correct when several people edit concurrently and
      absolute values would clobber each other.
    * ``quantity`` -- absolute set, e.g. a stock count after a physical audit.

    Exactly one must be supplied. ``reason`` is free text kept for the audit log.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    delta: int | None = Field(default=None, ge=-10_000_000, le=10_000_000)
    quantity: int | None = Field(default=None, ge=0, le=10_000_000)
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "StockAdjustment":
        if (self.delta is None) == (self.quantity is None):
            raise ValueError("supply exactly one of delta or quantity")
        if self.delta == 0:
            raise ValueError("delta cannot be zero; send no-op adjustments as nothing at all")
        return self

    @property
    def is_absolute(self) -> bool:
        return self.quantity is not None


class StockMovement(BaseModel):
    """Envelope ``data`` for a completed stock change.

    ``previous_quantity`` and ``delta`` are returned so a caller can verify the
    change landed without re-fetching the product.
    """

    product: ProductOut
    previous_quantity: int
    new_quantity: int
    delta: int
    reason: str | None = None


class InventoryItem(BaseModel):
    """One row of the stock report.

    Deliberately narrower than :class:`ProductOut`: an inventory screen needs the
    numbers and identifiers, not the full description on every row.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    category: str | None = None
    image_url: str
    price: Decimal
    stock_quantity: int
    in_stock: bool
    is_active: bool
    is_low_stock: bool


class InventoryPage(BaseModel):
    """Envelope ``data`` for the stock report.

    The counts describe the whole catalogue rather than the current page, so a
    header badge can show "3 out of 12 low" regardless of pagination.
    """

    products: list[InventoryItem]
    total: int
    page: int
    limit: int
    pages: int
    low_stock_threshold: int
    out_of_stock: int
    low_stock: int
    in_stock: int


__all__ = [
    "MAX_TAGS",
    "MAX_TAG_LENGTH",
    "InventoryItem",
    "InventoryPage",
    "ProductBase",
    "ProductCreate",
    "ProductOut",
    "ProductPage",
    "ProductUpdate",
    "SortOption",
    "StockAdjustment",
    "StockMovement",
]

"""Product model for the catalogue."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base


class Product(Base):
    # Fetch server-generated values (created_at, updated_at, and the
    # `onupdate` timestamp) with RETURNING during flush. Without this they
    # stay expired after a flush, and reading one in an async context raises
    # MissingGreenlet - a 503 in production.
    __mapper_args__ = {"eager_defaults": True}
    """A sellable catalogue item.

    Money is stored as :class:`~decimal.Decimal` (``NUMERIC(12, 2)``) rather than
    float, and the database enforces ``price > 0`` and ``mrp >= price`` so a
    discount can never be stored as a price increase.

    ``tags`` is a Postgres ``text[]`` with a GIN index: the frontend filters on
    individual tags, and a single array column keeps that a one-table query
    instead of a join table.

    ``rating`` and ``review_count`` are denormalised aggregates kept up to date
    by the reviews phase; they exist here so the catalogue can be rendered before
    any review exists.
    """

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("mrp IS NULL OR mrp >= price", name="mrp_gte_price"),
        CheckConstraint("stock_quantity >= 0", name="stock_non_negative"),
        CheckConstraint("review_count >= 0", name="review_count_non_negative"),
        CheckConstraint(
            "rating IS NULL OR (rating >= 0 AND rating <= 5)", name="rating_range"
        ),
        CheckConstraint("char_length(name) > 0", name="name_not_empty"),
        CheckConstraint("char_length(slug) > 0", name="slug_not_empty"),
        Index("ix_products_category_id", "category_id"),
        Index("ix_products_is_active", "is_active"),
        Index("ix_products_is_best_seller", "is_best_seller"),
        Index("ix_products_price", "price"),
        # Supports the default catalogue ordering (newest first) for active rows.
        Index("ix_products_active_created_at", "is_active", "created_at"),
        # GIN is what makes `tags @> ARRAY[...]` and `tags ?| ARRAY[...]` fast.
        Index("ix_products_tags", "tags", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    category_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("categories.id", ondelete="RESTRICT"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[str | None] = mapped_column(String(80), nullable=True)

    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    stock_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    image_url: Mapped[str] = mapped_column(String(500), nullable=False)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(50)), nullable=False, default=list, server_default="{}"
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_best_seller: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1), nullable=True)
    review_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    category: Mapped["Category"] = relationship(back_populates="products")  # noqa: F821

    @property
    def in_stock(self) -> bool:
        """Whether the item can be bought right now."""
        return self.stock_quantity > 0

    @property
    def discount_percent(self) -> int | None:
        """Whole-percent saving off ``mrp``, or ``None`` when not discounted."""
        if self.mrp is None or self.mrp <= self.price:
            return None
        return int(round((float(self.mrp - self.price) / float(self.mrp)) * 100))

    def __repr__(self) -> str:
        return f"<Product id={self.id} slug={self.slug!r} price={self.price}>"

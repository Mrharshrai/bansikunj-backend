"""Product category model."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base


class Category(Base):
    # Fetch server-generated values (created_at, updated_at, and the
    # `onupdate` timestamp) with RETURNING during flush. Without this they
    # stay expired after a flush, and reading one in an async context raises
    # MissingGreenlet - a 503 in production.
    __mapper_args__ = {"eager_defaults": True}
    """A product category, addressed publicly by its ``slug``.

    ``name`` and ``slug`` are both unique so a category can be looked up either
    way. ``is_active`` hides a category from the public catalogue without
    deleting rows that products may still reference.
    """

    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint("char_length(name) > 0", name="name_not_empty"),
        CheckConstraint("char_length(slug) > 0", name="slug_not_empty"),
        CheckConstraint("display_order >= 0", name="display_order_non_negative"),
        Index("ix_categories_is_active", "is_active"),
        Index("ix_categories_display_order", "display_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    display_order: Mapped[int] = mapped_column(
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

    products: Mapped[list["Product"]] = relationship(  # noqa: F821
        back_populates="category", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Category id={self.id} slug={self.slug!r}>"

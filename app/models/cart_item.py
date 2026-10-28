"""Cart line item model.

A cart is a set of ``CartItem`` rows owned by one user. There is no separate
``carts`` table: a user either has rows or has no cart, and the absence of rows
*is* the empty cart. That keeps the common case (add, change quantity, remove)
to a single-row read-modify-write and removes a whole table plus its lifecycle
from the checkout path.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base


class CartItem(Base):
    # Fetch server-generated values (created_at, updated_at, and the
    # `onupdate` timestamp) with RETURNING during flush. Without this they
    # stay expired after a flush, and reading one in an async context raises
    # MissingGreenlet - a 503 in production.
    __mapper_args__ = {"eager_defaults": True}
    """One product at one quantity in one user's cart.

    ``(user_id, product_id)`` is unique, so a product occupies at most one line
    per user and "add the same thing again" has to increment the existing row
    rather than create a duplicate. The name is spelled out because the metadata
    naming convention would otherwise derive it from ``user_id`` alone, which
    would be misleading for a two-column constraint.
    """

    __tablename__ = "cart_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        UniqueConstraint(
            "user_id", "product_id", name="uq_cart_items_user_id_product_id"
        ),
        Index("ix_cart_items_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    #: Eagerly loaded so a cart listing serialises without a query per line.
    product: Mapped["Product"] = relationship(lazy="selectin")  # noqa: F821

    @property
    def line_total(self) -> Decimal:
        """Quantity times the *current* product price.

        Computed on read rather than stored, so a cart can never hold a stale
        price after the catalogue changes. Prices are ``NUMERIC(12,2)``, so this
        is exact decimal arithmetic with no float rounding.
        """
        return self.product.price * self.quantity

    def __repr__(self) -> str:
        return (
            f"<CartItem user_id={self.user_id} "
            f"product_id={self.product_id} quantity={self.quantity}>"
        )

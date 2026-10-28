"""cart: per-user cart line items

Revision ID: 0003_cart
Revises: 0002_catalog
Create Date: 2026-09-27

A cart is a set of rows owned by one user; there is no ``carts`` table, so an
absent row set *is* the empty cart. ``(user_id, product_id)`` is unique, which is
what makes "add the same product again" an increment of the existing row rather
than a duplicate line.

Both foreign keys cascade: deleting a user takes their cart with them, and
deleting a product (Phase 3 delists rather than deletes, so this only fires for a
hard delete) must not leave an orphaned line that can never be rendered.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_cart"
down_revision: str | None = "0002_catalog"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cart_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("quantity > 0", name="ck_cart_items_quantity_positive"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_cart_items_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_cart_items_product_id_products",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cart_items"),
        # Spelled out in full: the metadata convention would otherwise derive a
        # name from `user_id` alone, which misdescribes a two-column constraint.
        sa.UniqueConstraint(
            "user_id", "product_id", name="uq_cart_items_user_id_product_id"
        ),
    )
    op.create_index("ix_cart_items_user_id", "cart_items", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_cart_items_user_id", table_name="cart_items")
    op.drop_table("cart_items")

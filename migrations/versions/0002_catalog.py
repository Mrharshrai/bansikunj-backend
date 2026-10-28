"""catalog: categories and products

Revision ID: 0002_catalog
Revises: 0001_init
Create Date: 2026-09-26

The public catalogue. Constraint and index names are spelled out in full because
Alembic emits literal DDL rather than going through ``Base.metadata``'s naming
convention; ``tests/test_migration_offline.py`` diffs this file against the
models so the two cannot drift apart silently.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_catalog"
down_revision: str | None = "0001_init"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("slug", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
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
        sa.CheckConstraint("char_length(name) > 0", name="ck_categories_name_not_empty"),
        sa.CheckConstraint("char_length(slug) > 0", name="ck_categories_slug_not_empty"),
        sa.CheckConstraint(
            "display_order >= 0", name="ck_categories_display_order_non_negative"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_categories"),
        sa.UniqueConstraint("name", name="uq_categories_name"),
        sa.UniqueConstraint("slug", name="uq_categories_slug"),
    )
    op.create_index("ix_categories_display_order", "categories", ["display_order"], unique=False)
    op.create_index("ix_categories_is_active", "categories", ["is_active"], unique=False)

    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("weight", sa.String(length=80), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("mrp", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("stock_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("image_url", sa.String(length=500), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.String(length=50)), server_default="{}", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_best_seller", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("rating", sa.Numeric(precision=2, scale=1), nullable=True),
        sa.Column("review_count", sa.Integer(), server_default="0", nullable=False),
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
        sa.CheckConstraint("price > 0", name="ck_products_price_positive"),
        sa.CheckConstraint(
            "mrp IS NULL OR mrp >= price", name="ck_products_mrp_gte_price"
        ),
        sa.CheckConstraint("stock_quantity >= 0", name="ck_products_stock_non_negative"),
        sa.CheckConstraint(
            "review_count >= 0", name="ck_products_review_count_non_negative"
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 0 AND rating <= 5)", name="ck_products_rating_range"
        ),
        sa.CheckConstraint("char_length(name) > 0", name="ck_products_name_not_empty"),
        sa.CheckConstraint("char_length(slug) > 0", name="ck_products_slug_not_empty"),
        # RESTRICT, not CASCADE: a category that still has products is a business
        # error the service reports as 409, and the database enforces it too.
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_products_category_id_categories",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("slug", name="uq_products_slug"),
    )
    op.create_index(
        "ix_products_active_created_at", "products", ["is_active", "created_at"], unique=False
    )
    op.create_index("ix_products_category_id", "products", ["category_id"], unique=False)
    op.create_index("ix_products_is_active", "products", ["is_active"], unique=False)
    op.create_index("ix_products_is_best_seller", "products", ["is_best_seller"], unique=False)
    op.create_index("ix_products_price", "products", ["price"], unique=False)
    # GIN is what makes the `tags @> ARRAY[...]` filter an index scan.
    op.create_index(
        "ix_products_tags",
        "products",
        ["tags"],
        unique=False,
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_products_tags", table_name="products")
    op.drop_index("ix_products_price", table_name="products")
    op.drop_index("ix_products_is_best_seller", table_name="products")
    op.drop_index("ix_products_is_active", table_name="products")
    op.drop_index("ix_products_category_id", table_name="products")
    op.drop_index("ix_products_active_created_at", table_name="products")
    op.drop_table("products")

    op.drop_index("ix_categories_is_active", table_name="categories")
    op.drop_index("ix_categories_display_order", table_name="categories")
    op.drop_table("categories")

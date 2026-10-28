"""banners: promotional carousel slides for the homepage

Revision ID: 0005_banners
Revises: 0004_orders
Create Date: 2026-10-07

A banner is deliberately standalone: no foreign keys, so a slide can outlive
whatever it happens to link to, and deleting one never touches a product or
category. ``image_url`` is NOT NULL - a banner without an image is not a
banner - while ``subtitle`` and ``link_url`` are optional copy.

``is_active`` hides a slide from the public carousel without deleting the row,
and the two indexes keep the homepage query (active, ordered) cheap as the
table grows.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_banners"
down_revision: str | None = "0004_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "banners",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(length=150), nullable=False),
        sa.Column("subtitle", sa.String(length=300), nullable=True),
        sa.Column("image_url", sa.String(length=500), nullable=False),
        sa.Column("link_url", sa.String(length=500), nullable=True),
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
        sa.CheckConstraint(
            "char_length(title) > 0", name="ck_banners_title_not_empty"
        ),
        sa.CheckConstraint(
            "display_order >= 0", name="ck_banners_display_order_non_negative"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_banners"),
    )
    op.create_index("ix_banners_is_active", "banners", ["is_active"], unique=False)
    op.create_index("ix_banners_display_order", "banners", ["display_order"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_banners_display_order", table_name="banners")
    op.drop_index("ix_banners_is_active", table_name="banners")
    op.drop_table("banners")

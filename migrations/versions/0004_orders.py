"""orders: order headers, snapshotted line items, and a status audit trail

Revision ID: 0004_orders
Revises: 0003_cart
Create Date: 2026-09-27

Two native enums. ``order_status`` is deliberately closed: the Angular client
types status as a five-value union and looks up a badge class through a total
``Record``, so any sixth value would render ``undefined`` as a CSS class. The
database is the right place to refuse it.

The money invariants are ``CHECK`` constraints rather than application logic, so
a bug in the service cannot persist an order whose parts do not add up to its
total (``total = subtotal + delivery_charge - discount``), nor a line whose
total disagrees with its own price and quantity
(``line_total = unit_price * quantity``). Both are exact on ``NUMERIC(12,2)``.

``order_items.product_id`` is ``RESTRICT`` while ``cart_items.product_id`` is
``CASCADE``: a cart line is disposable, an order line is charged money.
``order_status_history.changed_by_id`` is ``SET NULL`` so that deleting a user
keeps the audit trail of their orders.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_orders"
down_revision: str | None = "0003_cart"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    order_status = postgresql.ENUM(
        "pending",
        "confirmed",
        "shipped",
        "delivered",
        "cancelled",
        name="order_status",
        create_type=False,
    )
    order_status.create(op.get_bind(), checkfirst=True)

    payment_method = postgresql.ENUM(
        "cod",
        "online",
        name="payment_method",
        create_type=False,
    )
    payment_method.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            order_status,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("payment_method", payment_method, nullable=False),
        sa.Column("customer_name", sa.String(length=150), nullable=False),
        sa.Column("customer_email", sa.String(length=320), nullable=False),
        sa.Column("customer_phone", sa.String(length=20), nullable=False),
        sa.Column("address_line1", sa.String(length=255), nullable=False),
        sa.Column("address_line2", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("state", sa.String(length=100), nullable=False),
        sa.Column("pincode", sa.String(length=10), nullable=False),
        sa.Column("country", sa.String(length=2), server_default="IN", nullable=False),
        sa.Column("subtotal", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "delivery_charge",
            sa.Numeric(precision=12, scale=2),
            server_default="0.00",
            nullable=False,
        ),
        sa.Column(
            "discount",
            sa.Numeric(precision=12, scale=2),
            server_default="0.00",
            nullable=False,
        ),
        sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("subtotal >= 0", name="ck_orders_subtotal_non_negative"),
        sa.CheckConstraint(
            "delivery_charge >= 0", name="ck_orders_delivery_charge_non_negative"
        ),
        sa.CheckConstraint("discount >= 0", name="ck_orders_discount_non_negative"),
        sa.CheckConstraint("total >= 0", name="ck_orders_total_non_negative"),
        sa.CheckConstraint(
            "total = subtotal + delivery_charge - discount",
            name="ck_orders_total_consistent",
        ),
        sa.CheckConstraint(
            "char_length(customer_name) > 0", name="ck_orders_customer_name_not_empty"
        ),
        sa.CheckConstraint(
            "char_length(address_line1) > 0", name="ck_orders_address_line1_not_empty"
        ),
        sa.CheckConstraint(
            "char_length(pincode) = 6", name="ck_orders_pincode_is_six_digits"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_orders_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
    )
    op.create_index(
        "ix_orders_user_created_at", "orders", ["user_id", "created_at"], unique=False
    )
    op.create_index("ix_orders_status", "orders", ["status"], unique=False)

    op.create_table(
        "order_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Snapshots: what was bought, and for how much, at placement time.
        sa.Column("product_name", sa.String(length=200), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_total", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("unit_price > 0", name="ck_order_items_unit_price_positive"),
        sa.CheckConstraint(
            "line_total >= 0", name="ck_order_items_line_total_non_negative"
        ),
        sa.CheckConstraint(
            "line_total = unit_price * quantity",
            name="ck_order_items_line_total_consistent",
        ),
        sa.CheckConstraint(
            "char_length(product_name) > 0", name="ck_order_items_product_name_not_empty"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_items_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_order_items_product_id_products",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_items"),
    )
    op.create_index(
        "ix_order_items_order_id", "order_items", ["order_id"], unique=False
    )

    op.create_table(
        "order_status_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status", order_status, nullable=True),
        sa.Column("to_status", order_status, nullable=False),
        sa.Column("changed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.String(length=255), nullable=True),
        # clock_timestamp(), not now(): now() is the transaction start time, so
        # rows appended in one transaction would tie and the trail could not be
        # ordered by created_at.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_order_status_history_order_id_orders",
            ondelete="CASCADE",
        ),
        # SET NULL: a deleted user must not take the audit trail with them.
        sa.ForeignKeyConstraint(
            ["changed_by_id"],
            ["users.id"],
            name="fk_order_status_history_changed_by_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_status_history"),
    )
    op.create_index(
        "ix_order_status_history_order_id",
        "order_status_history",
        ["order_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_order_status_history_order_id", table_name="order_status_history")
    op.drop_table("order_status_history")
    op.drop_index("ix_order_items_order_id", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_status", table_name="orders")
    op.drop_index("ix_orders_user_created_at", table_name="orders")
    op.drop_table("orders")
    postgresql.ENUM(name="payment_method").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="order_status").drop(op.get_bind(), checkfirst=True)

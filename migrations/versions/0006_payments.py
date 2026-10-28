"""payments: Razorpay order, payment and refund state

Revision ID: 0006_payments
Revises: 0005_banners
Create Date: 2026-10-25

``orders.payment_status`` is a denormalised, list-friendly summary. The
``payments`` row is the provider-facing record: it snapshots the amount that was
sent to Razorpay and keeps the provider's order/payment/refund identifiers.

The provider identifiers are unique in the database. Webhooks are retried by
design, so "the same captured event arrives twice" is normal traffic; the
constraint is what makes the second write a no-op rather than a duplicate
financial record.

``not_required`` and ``not_started`` are order-only states. A payment row starts
at ``created``.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_payments"
down_revision: str | None = "0005_banners"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    payment_status = postgresql.ENUM(
        "not_required",
        "not_started",
        "created",
        "paid",
        "failed",
        "refunded",
        name="payment_status",
        create_type=False,
    )
    payment_status.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "orders",
        sa.Column(
            "payment_status",
            payment_status,
            server_default="not_started",
            nullable=False,
        ),
    )
    # Existing rows predate online payments. Anything already in the table was
    # placed as COD by the old flow, so mark it correctly rather than leaving
    # it as an unpaid online obligation.
    op.execute(
        "UPDATE orders SET payment_status = 'not_required' "
        "WHERE payment_method = 'cod'"
    )

    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "provider", sa.String(length=20), server_default="razorpay", nullable=False
        ),
        sa.Column("provider_order_id", sa.String(length=100), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=100), nullable=True),
        sa.Column("provider_refund_id", sa.String(length=100), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column(
            "currency", sa.String(length=3), server_default="INR", nullable=False
        ),
        sa.Column(
            "status", payment_status, server_default="created", nullable=False
        ),
        sa.Column(
            "signature_verified",
            sa.Boolean(),
            server_default="false",
            nullable=False,
        ),
        sa.Column("method", sa.String(length=50), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("failure_reason", sa.String(length=255), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("amount > 0", name="ck_payments_amount_positive"),
        sa.CheckConstraint(
            "char_length(currency) = 3", name="ck_payments_currency_is_three_letters"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name="fk_payments_order_id_orders",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_payments"),
        sa.UniqueConstraint("order_id", name="uq_payments_order_id"),
        sa.UniqueConstraint(
            "provider_order_id", name="uq_payments_provider_order_id"
        ),
        sa.UniqueConstraint(
            "provider_payment_id", name="uq_payments_provider_payment_id"
        ),
    )
    op.create_index("ix_payments_status", "payments", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_payments_status", table_name="payments")
    op.drop_table("payments")
    op.drop_column("orders", "payment_status")
    postgresql.ENUM(name="payment_status").drop(op.get_bind(), checkfirst=True)

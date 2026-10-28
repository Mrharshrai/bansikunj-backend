"""User model and role enum."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Index,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base


class Role(StrEnum):
    """Authorisation roles.

    Values are stored lowercase in Postgres so they match the API surface.
    Phase 8 replaces coarse role checks with a permission set; these four roles
    remain the source of truth.
    """

    CUSTOMER = "customer"
    ADMIN = "admin"
    STAFF = "staff"
    DELIVERY_AGENT = "delivery_agent"


role_enum = SAEnum(
    Role,
    name="user_role",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    native_enum=True,
)


class User(Base):
    # Fetch server-generated values (created_at, updated_at, and the
    # `onupdate` timestamp) with RETURNING during flush. Without this they
    # stay expired after a flush, and reading one in an async context raises
    # MissingGreenlet - a 503 in production.
    __mapper_args__ = {"eager_defaults": True}
    """An account. Passwords are stored only as Argon2id hashes."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("char_length(email) > 2", name="email_min_length"),
        CheckConstraint(
            "role IN ('customer', 'admin', 'staff', 'delivery_agent')",
            name="valid_role",
        ),
        Index("uq_users_email_lower", "email", unique=True),
        Index("ix_users_role", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)

    #: Argon2id hash. Never select this into an API response.
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[Role] = mapped_column(
        role_enum, nullable=False, default=Role.CUSTOMER, server_default=Role.CUSTOMER.value
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Single-use tokens, stored as SHA-256 digests.
    email_verification_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_reset_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    addresses: Mapped[list["Address"]] = relationship(  # noqa: F821
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(  # noqa: F821
        back_populates="user",
        cascade="all, delete-orphan",
        # "noload" is deprecated because it yields incorrect results for
        # unloaded collections. "raise_on_sql" keeps the same intent - never
        # emit an implicit SELECT - while staying correct. Plain "raise" would
        # break the delete-orphan cascade, which must traverse the collection.
        # Nothing in the app reads this relationship; token work goes through
        # RefreshTokenRepository, which queries directly.
        lazy="raise_on_sql",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} role={self.role}>"

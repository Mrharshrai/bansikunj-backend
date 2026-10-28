"""Server-side refresh-token store.

Access tokens are stateless, but refresh tokens must be revocable (logout) and
rotatable (reuse detection), so only a hash of each refresh token is persisted.
The raw token is returned to the client exactly once and never stored.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..core.database import Base


class RefreshToken(Base):
    # Fetch server-generated values (created_at, updated_at, and the
    # `onupdate` timestamp) with RETURNING during flush. Without this they
    # stay expired after a flush, and reading one in an async context raises
    # MissingGreenlet - a 503 in production.
    __mapper_args__ = {"eager_defaults": True}
    """One issued refresh token, identified by its JWT ``jti`` claim."""

    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id", "user_id"),
        Index("ix_refresh_tokens_jti", "jti", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The JWT ``jti``; unique so a replayed token is detectable.
    jti: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replaced_by_jti: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")  # noqa: F821

    def __repr__(self) -> str:
        return f"<RefreshToken jti={self.jti} user_id={self.user_id}>"

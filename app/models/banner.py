"""Promotional banner model."""
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
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.database import Base


class Banner(Base):
    # Fetch server-generated values (created_at, updated_at, and the
    # `onupdate` timestamp) with RETURNING during flush. Without this they
    # stay expired after a flush, and reading one in an async context raises
    # MissingGreenlet - a 503 in production.
    __mapper_args__ = {"eager_defaults": True}
    """A homepage carousel slide.

    ``image_url`` is required: a banner without an image is not a banner.
    ``is_active`` hides a slide from the public carousel without deleting the
    row, and ``display_order`` decides where it sits in the rotation.
    """

    __tablename__ = "banners"
    __table_args__ = (
        CheckConstraint("char_length(title) > 0", name="title_not_empty"),
        CheckConstraint("display_order >= 0", name="display_order_non_negative"),
        Index("ix_banners_is_active", "is_active"),
        Index("ix_banners_display_order", "display_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    title: Mapped[str] = mapped_column(String(150), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(300), nullable=True)
    image_url: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Where the slide links when tapped - a root-relative path or an http(s) URL.
    link_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

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

    def __repr__(self) -> str:
        return f"<Banner id={self.id} title={self.title!r}>"

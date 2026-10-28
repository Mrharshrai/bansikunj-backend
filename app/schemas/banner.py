"""Banner request/response schemas."""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _check_text(value: str | None, *, field: str) -> str | None:
    """Trim ``value`` and refuse a blank one.

    ``None`` passes through so the update schema can leave a field unchanged
    (or, for the nullable fields, clear it).
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError(f"{field} cannot be blank")
    return value


def _check_required_text(value: str | None, *, field: str) -> str:
    """Like :func:`_check_text` but ``None`` is also rejected."""
    if value is None:
        raise ValueError(f"{field} is required")
    result = _check_text(value, field=field)
    assert result is not None
    return result


def _check_image_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("image_url cannot be blank")
    if not value.startswith(("http://", "https://", "/")):
        raise ValueError(
            "image_url must be an http(s) URL or a root-relative path, e.g. '/assets/banner.png'"
        )
    return value


def _check_required_image_url(value: str | None) -> str:
    if value is None:
        raise ValueError("image_url is required")
    result = _check_image_url(value)
    assert result is not None
    return result


def _check_link_url(value: str | None) -> str | None:
    """``None`` clears the link; a value must still look like a URL or path."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("link_url cannot be blank; use null to remove the link")
    if not value.startswith(("http://", "https://", "/")):
        raise ValueError(
            "link_url must be an http(s) URL or a root-relative path, e.g. '/products'"
        )
    return value


class _BannerValidators:
    """Validators shared by the create and update schemas.

    ``title`` and ``image_url`` never accept ``None``: a banner without either
    is not renderable, so an explicit ``null`` is a 422 rather than a silent
    NULL in the database. An *absent* field on update is never validated (the
    default is not run), so it still means "leave unchanged".
    """

    @field_validator("title")
    @classmethod
    def _title(cls, value: str | None) -> str:
        return _check_required_text(value, field="title")

    @field_validator("image_url")
    @classmethod
    def _image(cls, value: str | None) -> str:
        return _check_required_image_url(value)

    @field_validator("subtitle")
    @classmethod
    def _subtitle(cls, value: str | None) -> str | None:
        return _check_text(value, field="subtitle")

    @field_validator("link_url")
    @classmethod
    def _link(cls, value: str | None) -> str | None:
        return _check_link_url(value)


class BannerBase(_BannerValidators, BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=2, max_length=150)
    subtitle: str | None = Field(default=None, max_length=300)
    image_url: str = Field(max_length=500)
    link_url: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    display_order: int = Field(default=0, ge=0, le=100_000)


class BannerCreate(BannerBase):
    """Creating a banner."""


class BannerUpdate(_BannerValidators, BaseModel):
    """Partial update; only the fields present in the body are changed."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=2, max_length=150)
    subtitle: str | None = Field(default=None, max_length=300)
    image_url: str | None = Field(default=None, max_length=500)
    link_url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=100_000)


class BannerOut(BaseModel):
    """A banner as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    subtitle: str | None = None
    image_url: str
    link_url: str | None = None
    is_active: bool
    display_order: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, banner) -> "BannerOut":
        return cls(
            id=banner.id,
            title=banner.title,
            subtitle=banner.subtitle,
            image_url=banner.image_url,
            link_url=banner.link_url,
            is_active=banner.is_active,
            display_order=banner.display_order,
            created_at=banner.created_at,
            updated_at=banner.updated_at,
        )


__all__ = [
    "BannerBase",
    "BannerCreate",
    "BannerOut",
    "BannerUpdate",
]

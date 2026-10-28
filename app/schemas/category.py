"""Category request/response schemas."""
from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalise_slug(value: str | None) -> str | None:
    """Lowercase and validate a client-supplied slug.

    Returns ``None`` for a blank value so the caller can fall back to generating
    one from the name.
    """
    if value is None or not value.strip():
        return None
    value = value.strip().lower()
    if not SLUG_PATTERN.match(value):
        raise ValueError(
            "slug must be lowercase letters, digits and single hyphens, e.g. 'dark-chocolate'"
        )
    return value


def _check_name(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("Category name cannot be blank")
    return value


def _check_image_url(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    allowed = value.startswith(("http://", "https://", "/"))
    if not allowed:
        raise ValueError(
            "image_url must be an http(s) URL or a root-relative path, e.g. '/assets/cake.png'"
        )
    return value


class _CategoryValidators:
    """Validators shared by the create and update schemas.

    Every one of them tolerates ``None`` because the same mixin serves both the
    create schema (where the fields are required) and the update schema (where
    they are optional and absent fields mean "leave unchanged").
    """

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return _check_name(value)

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        return normalise_slug(value)

    @field_validator("image_url")
    @classmethod
    def _image(cls, value: str | None) -> str | None:
        return _check_image_url(value)


class CategoryBase(_CategoryValidators, BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=150)
    #: Derived from ``name`` by the service when omitted.
    slug: str | None = Field(default=None, max_length=150)
    description: str | None = Field(default=None, max_length=2000)
    image_url: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    display_order: int = Field(default=0, ge=0, le=100_000)


class CategoryCreate(CategoryBase):
    """Creating a category."""


class CategoryUpdate(_CategoryValidators, BaseModel):
    """Partial update; only the fields present in the body are changed."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=150)
    slug: str | None = Field(default=None, max_length=150)
    description: str | None = Field(default=None, max_length=2000)
    image_url: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None
    display_order: int | None = Field(default=None, ge=0, le=100_000)


class CategoryOut(BaseModel):
    """A category as returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    slug: str
    description: str | None = None
    image_url: str | None = None
    is_active: bool
    display_order: int
    product_count: int = 0
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, category, *, product_count: int = 0) -> "CategoryOut":
        """Build the response body from an ORM row.

        ``product_count`` is passed in rather than read off the relationship so
        the router can supply a single aggregated count for the whole page.
        """
        return cls(
            id=category.id,
            name=category.name,
            slug=category.slug,
            description=category.description,
            image_url=category.image_url,
            is_active=category.is_active,
            display_order=category.display_order,
            product_count=product_count,
            created_at=category.created_at,
            updated_at=category.updated_at,
        )


__all__ = [
    "SLUG_PATTERN",
    "CategoryBase",
    "CategoryCreate",
    "CategoryOut",
    "CategoryUpdate",
    "normalise_slug",
]

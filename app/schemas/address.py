"""Address schemas."""
from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

PINCODE_PATTERN = re.compile(r"^[A-Za-z0-9\- ]{4,10}$")


class AddressBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(default="Home", max_length=40)
    full_name: str = Field(min_length=1, max_length=150)
    phone: str = Field(min_length=10, max_length=20)
    line1: str = Field(min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    pincode: str = Field(min_length=4, max_length=10)
    country: str = Field(default="IN", min_length=2, max_length=2)
    is_default: bool = False

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value: str) -> str:
        cleaned = re.sub(r"[\s\-()]", "", value)
        if not cleaned.isdigit() or not (10 <= len(cleaned) <= 15):
            raise ValueError("Phone number must be 10-15 digits")
        return cleaned

    @field_validator("pincode")
    @classmethod
    def _check_pincode(cls, value: str) -> str:
        if not PINCODE_PATTERN.match(value):
            raise ValueError("Pincode contains invalid characters")
        return value

    @field_validator("country")
    @classmethod
    def _upper_country(cls, value: str) -> str:
        return value.upper()

    @field_validator("full_name", "line1", "city", "state")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Field cannot be blank")
        return value.strip()


class AddressCreate(AddressBase):
    pass


class AddressUpdate(BaseModel):
    """All fields optional; only provided keys are applied."""

    model_config = ConfigDict(str_strip_whitespace=True)

    label: str | None = Field(default=None, max_length=40)
    full_name: str | None = Field(default=None, min_length=1, max_length=150)
    phone: str | None = Field(default=None, min_length=10, max_length=20)
    line1: str | None = Field(default=None, min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, min_length=1, max_length=100)
    state: str | None = Field(default=None, min_length=1, max_length=100)
    pincode: str | None = Field(default=None, min_length=4, max_length=10)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    is_default: bool | None = None

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = re.sub(r"[\s\-()]", "", value)
        if not cleaned.isdigit() or not (10 <= len(cleaned) <= 15):
            raise ValueError("Phone number must be 10-15 digits")
        return cleaned

    @field_validator("country")
    @classmethod
    def _upper_country(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class AddressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    label: str
    full_name: str
    phone: str
    line1: str
    line2: str | None = None
    city: str
    state: str
    pincode: str
    country: str
    is_default: bool
    created_at: datetime
    updated_at: datetime


class AddressWithUser(AddressOut):
    """Admin-scoped projection that also identifies the owning user."""

    user_email: str
    user_full_name: str

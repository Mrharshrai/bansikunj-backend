"""User profile schemas."""
from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from ..models.user import Role


class UserOut(BaseModel):
    """Public representation of a user.

    Constructed from an explicit projection - there is no ``hashed_password``
    field, so a hash cannot reach a client even if the ORM object is passed in.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    phone: str | None = None
    role: Role
    is_active: bool
    is_email_verified: bool
    created_at: datetime
    updated_at: datetime


class UserProfileUpdate(BaseModel):
    """Partial profile update. Email changes are deliberately not allowed here."""

    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: str | None = Field(default=None, min_length=1, max_length=150)
    phone: str | None = Field(default=None, max_length=20)

    @field_validator("full_name")
    @classmethod
    def _check_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("Full name cannot be blank")
        return value.strip()

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = re.sub(r"[\s\-()]", "", value)
        if not cleaned.isdigit() or not (10 <= len(cleaned) <= 15):
            raise ValueError("Phone number must be 10-15 digits")
        return cleaned


class UserRoleUpdate(BaseModel):
    """Admin-only role change (Phase 8 exposes the endpoint; schema lives here)."""

    role: Role


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)

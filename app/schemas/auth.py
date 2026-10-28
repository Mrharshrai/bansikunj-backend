"""Authentication request/response schemas."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from ..models.user import Role

PASSWORD_MIN_LENGTH = 8

#: At least one letter and one digit; length is enforced separately.
PASSWORD_PATTERN = re.compile(r"^(?=.*[A-Za-z])(?=.*\d).+$")


def _validate_password_strength(value: str) -> str:
    if not PASSWORD_PATTERN.match(value):
        raise ValueError("Password must contain at least one letter and one digit")
    return value


StrongPassword = Annotated[str, Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)]


class RegisterRequest(BaseModel):
    """New account. The role is never accepted from the client."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    password: StrongPassword
    full_name: str = Field(min_length=1, max_length=150)
    phone: str | None = Field(default=None, max_length=20)

    @field_validator("password")
    @classmethod
    def _check_strength(cls, value: str) -> str:
        return _validate_password_strength(value)

    @field_validator("full_name")
    @classmethod
    def _check_name(cls, value: str) -> str:
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


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class AuthTokenPair(BaseModel):
    """Issued token pair. Never contains the password hash."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="Access token lifetime in seconds")
    expires_at: datetime


class UserSummary(BaseModel):
    """Minimal user projection safe for any authenticated caller."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: Role
    is_email_verified: bool


class TokenBundle(BaseModel):
    tokens: AuthTokenPair


class AuthBundle(BaseModel):
    """``data`` payload for register and login.

    The verification token is never returned: it is delivered by ``EmailService``
    so a caller cannot read another account's proof of ownership from a response.
    """

    user: UserSummary
    tokens: AuthTokenPair


class RegisterResponse(BaseModel):
    user: UserSummary
    tokens: AuthTokenPair


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: StrongPassword

    @field_validator("new_password")
    @classmethod
    def _check_strength(cls, value: str) -> str:
        return _validate_password_strength(value)


class VerifyEmailRequest(BaseModel):
    token: str = Field(min_length=1)


class EmailVerificationRequest(BaseModel):
    """Re-send a verification email to the authenticated user."""

    pass


class MessageResponse(BaseModel):
    """Envelope for endpoints that return only a status message."""

    message: str

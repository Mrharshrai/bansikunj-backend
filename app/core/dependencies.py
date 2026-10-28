"""Reusable FastAPI dependencies.

Auth dependencies resolve the caller once per request and expose them as
``CurrentUser``. Route handlers never inspect roles themselves; they declare
``Depends(require_role(Role.ADMIN))`` instead, so authorisation lives in one
place and cannot be forgotten inconsistently.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.user import Role, User
from ..services.auth_service import AuthService
from ..services.email_service import EmailService, get_email_service
from .config import Settings, get_settings
from .database import get_db
from .exceptions import ForbiddenError, UnauthorizedError
from .security import TokenError, TokenType, decode_token

bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="Paste the access token returned by /auth/login.",
)

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSessionDep = Annotated[AsyncSession, Depends(get_db)]


EmailServiceDep = Annotated[EmailService, Depends(get_email_service)]


def get_auth_service(
    session: DbSessionDep, email_service: EmailServiceDep
) -> AuthService:
    """Build the auth service for the request's session and mail transport."""
    return AuthService(session, email_service=email_service)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


def _extract_bearer_token(
    credentials: HTTPAuthorizationCredentials | None,
) -> str:
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError(
            "Not authenticated", error_code="missing_credentials"
        )
    return credentials.credentials


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    auth: AuthServiceDep,
) -> User:
    """Resolve the authenticated, active user or raise 401/403."""
    token = _extract_bearer_token(credentials)
    try:
        payload = decode_token(token, expected_type=TokenType.ACCESS)
    except TokenError as exc:
        raise UnauthorizedError(str(exc), error_code="invalid_token") from exc

    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("Token is invalid", error_code="invalid_token") from exc

    user = await auth.users.get_by_id(user_id)
    if user is None:
        raise UnauthorizedError("Account no longer exists", error_code="unknown_user")
    if not user.is_active:
        raise ForbiddenError(
            "This account has been deactivated", error_code="account_inactive"
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_optional_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    auth: AuthServiceDep,
) -> User | None:
    """Like :func:`get_current_user` but returns ``None`` for anonymous callers."""
    if credentials is None or not credentials.credentials:
        return None
    try:
        payload = decode_token(credentials.credentials, expected_type=TokenType.ACCESS)
        user_id = uuid.UUID(str(payload["sub"]))
    except (TokenError, KeyError, ValueError):
        return None
    user = await auth.users.get_by_id(user_id)
    return user if user is not None and user.is_active else None


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def require_role(
    *allowed: Role,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """Dependency factory enforcing that the caller holds one of ``allowed``.

    Usage::

        @router.delete("/{product_id}", dependencies=[Depends(require_role(Role.ADMIN))])

        # or, when the handler needs the user object:
        @router.get("/reports")
        async def reports(user: Annotated[User, Depends(require_role(Role.ADMIN, Role.STAFF))]):
            ...

    Raises:
        ForbiddenError: 403 when the caller's role is not permitted.
    """
    permitted = frozenset(allowed)
    if not permitted:
        raise ValueError("require_role() needs at least one role")

    async def _dependency(user: CurrentUser) -> User:
        if user.role not in permitted:
            raise ForbiddenError(
                "You do not have permission to perform this action",
                error_code="insufficient_role",
                details={"required_roles": sorted(role.value for role in permitted)},
            )
        return user

    role_names = ", ".join(sorted(role.value for role in permitted))
    _dependency.__name__ = f"require_role_{'_'.join(sorted(r.value for r in permitted))}"
    _dependency.__doc__ = f"Require one of these roles: {role_names}."
    return _dependency


require_admin = require_role(Role.ADMIN)
require_staff = require_role(Role.ADMIN, Role.STAFF)

__all__ = [
    "SettingsDep",
    "DbSessionDep",
    "AuthServiceDep",
    "EmailServiceDep",
    "CurrentUser",
    "OptionalUser",
    "bearer_scheme",
    "get_settings",
    "get_db",
    "get_auth_service",
    "get_current_user",
    "get_optional_user",
    "require_role",
    "require_admin",
    "require_staff",
]

"""Application error types and their HTTP mapping.

Services raise these; :mod:`app.main` installs one handler per type and renders
the standard error envelope. Raising an ``AppError`` from anywhere inside the
request path is the supported way to return a clean, coded error to a client.

Note: the Pydantic exception is also called ``ValidationError``; in modules that
need both, import Pydantic's as ``PydanticValidationError`` to avoid ambiguity.
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for expected, client-visible application errors."""

    status_code: int = 400
    error_code: str = "bad_request"
    default_message: str = "Bad request"

    def __init__(
        self,
        message: str | None = None,
        *,
        error_code: str | None = None,
        status_code: int | None = None,
        details: Any | None = None,
    ) -> None:
        self.message = message or self.default_message
        if error_code is not None:
            self.error_code = error_code
        if status_code is not None:
            self.status_code = status_code
        self.details = details
        super().__init__(self.message)


class BadRequestError(AppError):
    """The request is syntactically valid but cannot be processed as sent."""

    status_code = 400
    error_code = "bad_request"
    default_message = "Bad request"


class ValidationError(AppError):
    """Input failed business or schema validation."""

    status_code = 422
    error_code = "validation_error"
    default_message = "The submitted data is invalid"


class NotFoundError(AppError):
    """A requested resource does not exist."""

    status_code = 404
    error_code = "not_found"
    default_message = "Resource not found"


class ConflictError(AppError):
    """The request conflicts with the current state of the resource."""

    status_code = 409
    error_code = "conflict"
    default_message = "Resource conflict"


class UnauthorizedError(AppError):
    """Authentication is missing or invalid."""

    status_code = 401
    error_code = "unauthorized"
    default_message = "Not authenticated"


class ForbiddenError(AppError):
    """Authenticated, but not permitted to perform this action."""

    status_code = 403
    error_code = "forbidden"
    default_message = "You do not have permission to perform this action"


class ServiceUnavailableError(AppError):
    """A backing dependency (database, cache, storage) is unavailable."""

    status_code = 503
    error_code = "service_unavailable"
    default_message = "Service temporarily unavailable"

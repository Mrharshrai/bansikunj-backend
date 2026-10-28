"""Standard success/error response envelope.

Success: ``{"success": true,  "message": str, "data": ...}``
Error:   ``{"success": false, "message": str, "error_code": str, "details": ...}``

Every endpoint in the project returns one of these two shapes, so clients can
branch on ``success`` alone.
"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

T = TypeVar("T")


class SuccessEnvelope(BaseModel, Generic[T]):
    """Success payload wrapper."""

    success: bool = True
    message: str
    data: T | None = None


class ErrorEnvelope(BaseModel):
    """Error payload wrapper."""

    success: bool = False
    message: str
    error_code: str
    details: Any | None = None


def success_response(
    data: Any = None,
    message: str = "OK",
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a success ``JSONResponse`` in the standard envelope."""
    body = SuccessEnvelope(success=True, message=message, data=data)
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body),
        headers=headers,
    )


def error_response(
    message: str,
    error_code: str,
    details: Any | None = None,
    *,
    status_code: int = 400,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build an error ``JSONResponse`` in the standard envelope.

    ``message`` is always safe to show a client; raw exception text belongs in
    the server log, not here.
    """
    body = ErrorEnvelope(
        success=False, message=message, error_code=error_code, details=details
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body),
        headers=headers,
    )


def paginated_envelope(
    items: list[Any],
    *,
    page: int,
    limit: int,
    total: int,
    message: str = "OK",
) -> dict[str, Any]:
    """Success envelope for a paginated collection.

    TODO Phase 3: the pagination utility proper lands with the product catalogue.
    """
    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "success": True,
        "message": message,
        "data": {
            "items": items,
            "page": page,
            "limit": limit,
            "total": total,
            "total_pages": total_pages,
        },
    }

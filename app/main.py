"""FastAPI application entry point.

Wires configuration, structured logging, CORS, the standard response envelope,
global exception handlers, and the Phase 1 health endpoints.

Routes added in later phases are included under ``settings.API_V1_PREFIX``.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .core.config import Settings, get_settings
from .core.database import check_database_connection, dispose_engine
from .core.envelope import error_response, success_response
from .core.exceptions import AppError, ServiceUnavailableError
from .core.logging import configure_logging, get_logger

#: Human-readable mapping of HTTP status -> stable machine error code.
STATUS_ERROR_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_server_error",
    503: "service_unavailable",
}


def _error_code_for(status_code: int) -> str:
    return STATUS_ERROR_CODES.get(status_code, "error")


def _flatten_validation_errors(
    errors: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Convert Pydantic/FastAPI error dicts into a JSON-safe, presentable list.

    Takes the raw ``.errors()`` mappings rather than an exception object:
    rebuilding a ``PydanticValidationError`` via ``from_exception_data`` to read
    them straight back out re-runs Pydantic's error validation, and error types
    that carry a ``ctx`` (such as email-validator's ``value_error``, which
    requires an ``error`` key) raise ``ValueError: 'error' required in
    context``. That turned every invalid email into an unhandled 500.
    """
    flattened: list[dict[str, Any]] = []
    for error in errors:
        flattened.append(
            {
                "field": ".".join(str(part) for part in error.get("loc", ())) or None,
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type"),
            }
        )
    return flattened


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Configure logging on startup and release DB connections on shutdown."""
    settings: Settings = application.state.settings
    configure_logging(
        level=settings.LOG_LEVEL,
        fmt=settings.LOG_FORMAT,
        service_name=settings.APP_NAME,
    )
    settings.assert_production_safe()
    log = get_logger(__name__, environment=settings.ENVIRONMENT)
    log.info(
        "application.startup",
        version=settings.APP_VERSION,
        docs_url="/docs",
        log_format=settings.LOG_FORMAT,
    )
    try:
        yield
    finally:
        await dispose_engine()
        log.info("application.shutdown")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a request id to the log context and emit one access log line."""

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("x-request-id") or uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers["x-request-id"] = request_id
        get_logger("app.access").info(
            "request.completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
        )
        structlog.contextvars.clear_contextvars()
        return response


def register_exception_handlers(application: FastAPI) -> None:
    """Install handlers that render every error in the standard envelope."""

    @application.exception_handler(AppError)
    async def _handle_app_error(
        request: Request, exc: AppError
    ) -> JSONResponse:
        get_logger(__name__).warning(
            "request.app_error",
            error_code=exc.error_code,
            status_code=exc.status_code,
            path=request.url.path,
            details=exc.details,
        )
        return error_response(
            message=exc.message,
            error_code=exc.error_code,
            details=exc.details,
            status_code=exc.status_code,
        )

    @application.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        get_logger(__name__).info(
            "request.validation_error",
            path=request.url.path,
            error_count=len(exc.errors()),
        )
        return error_response(
            message="Request validation failed",
            error_code="validation_error",
            details=_flatten_validation_errors(exc.errors()),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @application.exception_handler(PydanticValidationError)
    async def _handle_pydantic_validation(
        request: Request, exc: PydanticValidationError
    ) -> JSONResponse:
        get_logger(__name__).info(
            "request.pydantic_validation_error", path=request.url.path
        )
        return error_response(
            message="Request validation failed",
            error_code="validation_error",
            details=_flatten_validation_errors(exc.errors()),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    @application.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return error_response(
            message=str(exc.detail),
            error_code=_error_code_for(exc.status_code),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @application.exception_handler(SQLAlchemyError)
    async def _handle_database_error(
        request: Request, exc: SQLAlchemyError
    ) -> JSONResponse:
        # The real cause is logged server-side; the client learns nothing about
        # the schema, connection string, or driver internals.
        get_logger(__name__).error(
            "request.database_error",
            path=request.url.path,
            exc_info=exc,
        )
        wrapped = ServiceUnavailableError(
            "The service is temporarily unable to handle the request"
        )
        return error_response(
            message=wrapped.message,
            error_code=wrapped.error_code,
            status_code=wrapped.status_code,
        )

    @application.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        get_logger(__name__).error(
            "request.unhandled_exception",
            path=request.url.path,
            exc_info=exc,
        )
        return error_response(
            message="An unexpected error occurred",
            error_code="internal_server_error",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Application factory."""
    resolved = settings or get_settings()

    application = FastAPI(
        title=resolved.APP_NAME,
        version=resolved.APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={"name": "Bansikunj", "email": "hello@bansikunj.in"},
        license_info={"name": "MIT"},
    )
    application.state.settings = resolved

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["x-request-id"],
    )
    application.add_middleware(RequestContextMiddleware)

    register_exception_handlers(application)

    # Business routers (Phase 2+). Mounted under the versioned prefix.
    from .api.v1.router import api_router

    application.include_router(api_router, prefix=resolved.API_V1_PREFIX)

    @application.get("/health", tags=["health"], summary="Liveness probe")
    async def health() -> JSONResponse:
        """Process-level liveness. Does not touch the database."""
        return success_response(
            data={
                "status": "ok",
                "service": resolved.APP_NAME,
                "version": resolved.APP_VERSION,
                "environment": resolved.ENVIRONMENT,
            },
            message="Service is healthy",
        )

    @application.get("/health/db", tags=["health"], summary="Readiness probe")
    async def health_db() -> JSONResponse:
        """Readiness probe. Executes ``SELECT 1`` and reports DB latency."""
        try:
            details = await check_database_connection()
        except SQLAlchemyError as exc:
            get_logger(__name__).error("health.db_unavailable", exc_info=exc)
            return error_response(
                message="Database is unavailable",
                error_code="service_unavailable",
                details={"status": "unavailable"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return success_response(data=details, message="Database is healthy")

    return application


app = create_app()

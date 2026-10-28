"""Structured logging setup with secret redaction.

All application and third-party logs are routed through ``structlog`` so records
are machine-parseable JSON in production and human-readable in development.

Security: :func:`redact_secrets` is registered as a structlog processor, so it
applies to bound context variables *and* to the event dictionary. Anything whose
key looks sensitive is masked, and database URLs are masked even when they arrive
as part of a free-form message.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Iterable

import structlog

#: Substrings that mark a mapping key as sensitive (compared case-insensitively).
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "auth_header",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "cookie",
    "session_id",
    "signature",
    "credential",
    "dsn",
    "database_url",
    "connection_string",
)

REDACTED = "***REDACTED***"

#: Marker used for keys that are secret but still worth showing the shape of.
_EXACT_KEYS = frozenset({"iat", "exp"})


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if lowered in _EXACT_KEYS:
        return False
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _mask_url_credentials(value: str) -> str:
    """Replace the password in a URL-ish string with ``***``."""
    if "://" not in value or "@" not in value:
        return value
    scheme, _, remainder = value.partition("://")
    credentials, _, host = remainder.rpartition("@")
    if not credentials or ":" not in credentials:
        return value
    user, _, _password = credentials.partition(":")
    return f"{scheme}://{user}:***@{host}"


def _scrub_string(value: str) -> str:
    scrubbed = _mask_url_credentials(value)
    lowered = scrubbed.lower()
    if "://" in lowered and "postgres" in lowered:
        return _mask_url_credentials(scrubbed)
    return scrubbed


def redact_secrets(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor that masks sensitive values anywhere in the record."""
    for key, value in list(event_dict.items()):
        if _is_sensitive_key(key):
            event_dict[key] = REDACTED
        elif isinstance(value, str):
            event_dict[key] = _scrub_string(value)
        elif isinstance(value, dict):
            event_dict[key] = redact_secrets(_logger, _method_name, value)
        elif isinstance(value, (list, tuple)):
            event_dict[key] = [
                redact_secrets(_logger, _method_name, item)
                if isinstance(item, dict)
                else (_scrub_string(item) if isinstance(item, str) else item)
                for item in value
            ]
    return event_dict


def add_log_level(_logger: Any, _method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Attach the stdlib log level to the structlog record."""
    if isinstance(event_dict.get("level"), str):
        return event_dict
    level = event_dict.get("level_number")
    if level is None:
        return event_dict
    event_dict["level"] = logging.getLevelName(level).lower()
    return event_dict


def configure_logging(
    level: str = "INFO", fmt: str = "console", service_name: str = "bansikunj-api"
) -> None:
    """Configure stdlib logging and structlog for the whole process.

    Args:
        level: Log level name, e.g. ``"DEBUG"``.
        fmt: ``"console"`` for human-readable output, ``"json"`` for production.
        service_name: Value bound to every record as ``service``.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        redact_secrets,
    ]

    renderer: Any
    if fmt == "json":
        renderer = structlog.processors.JSONRenderer(sort_keys=True)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.ExceptionPrettyPrinter()
            if fmt != "json"
            else structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    # Uvicorn installs its own colourised handlers; route them through ours so
    # access logs are structured and pass through redaction too.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    # SQLAlchemy echoes full statements (and therefore bound parameters).
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if numeric_level <= logging.DEBUG else logging.WARNING
    )

    structlog.get_logger().bind(service=service_name)


def get_logger(name: str | None = None, **initial_values: Any) -> Any:
    """Return a bound structlog logger."""
    return structlog.get_logger(name, **initial_values)


def loggable_keys() -> Iterable[str]:
    """Keys that are safe to attach to logs (helper for future service code)."""
    return ("user_id", "order_id", "product_id", "request_id", "status", "amount")

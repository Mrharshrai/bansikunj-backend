"""Application configuration.

Every sensitive value is read from the environment (``.env`` in development) and is
typed as :class:`pydantic.SecretStr` so it cannot be leaked by an accidental
``repr()``, log call, or ``model_dump()``.

Secrets intentionally have **no defaults**: a missing secret must fail loudly at
startup rather than silently fall back to a well-known value. ``POSTGRES_PASSWORD``
is the exception because ``DATABASE_URL`` may carry the credentials instead.
"""
from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .logging import get_logger

Environment = Literal["local", "dev", "staging", "production"]


def _normalise_database_url(raw: str) -> str:
    """Return an asyncpg-compatible DSN.

    Hosted providers hand out libpq-style URLs. Neon, for example, uses
    ``postgresql://`` with ``sslmode`` and ``channel_binding`` query
    parameters, neither of which the asyncpg dialect understands as-is.
    """
    parts = urlsplit(raw.strip())
    scheme = parts.scheme.lower()
    if scheme in {"postgres", "postgresql"}:
        scheme = "postgresql+asyncpg"

    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    sslmode = query.pop("sslmode", None)
    query.pop("channel_binding", None)
    query.pop("pgbouncer", None)

    if query.get("ssl", "").lower() == "disable":
        query.pop("ssl")
    elif "ssl" not in query and sslmode and sslmode.lower() != "disable":
        query["ssl"] = "require"
    elif (
        "ssl" not in query
        and not sslmode
        and (parts.hostname or "").lower().endswith(".neon.tech")
    ):
        query["ssl"] = "require"

    if "connect_timeout" in query and "timeout" not in query:
        query["timeout"] = query.pop("connect_timeout")

    return urlunsplit(
        (scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


class Settings(BaseSettings):
    """Typed application settings sourced from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ app
    APP_NAME: str = "Bansikunj API"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: Environment = "local"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # ----------------------------------------------------------- postgres
    POSTGRES_USER: str = "postgres"
    # Optional when DATABASE_URL is provided (the normal hosted deployment).
    POSTGRES_PASSWORD: SecretStr | None = None
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = Field(default=5432, ge=1, le=65535)
    POSTGRES_DB: str = "bansikunj"
    # Optional full-URL override; wins over the discrete parts above when set.
    DATABASE_URL: SecretStr | None = None

    DB_POOL_SIZE: int = Field(default=5, ge=1)
    DB_MAX_OVERFLOW: int = Field(default=10, ge=0)
    DB_POOL_TIMEOUT: int = Field(default=30, ge=1)
    DB_POOL_RECYCLE: int = Field(default=1800, ge=1)
    # Reconnect when a hosted database (Neon) has suspended an idle connection.
    DB_POOL_PRE_PING: bool = True
    DB_ECHO: bool = False
    DB_CONNECT_TIMEOUT: int = Field(default=10, ge=1)

    # ---------------------------------------------------------------- jwt
    # TODO Phase 2: consumed by the token service (access + refresh issuance).
    JWT_SECRET_KEY: SecretStr = Field(min_length=32)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, ge=1)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=14, ge=1)

    # ------------------------------------------------------------ password
    # TODO Phase 2: Argon2 parameters for the password hasher.
    PASSWORD_ARGON2_TIME_COST: int = Field(default=3, ge=1)
    PASSWORD_ARGON2_MEMORY_COST_KIB: int = Field(default=65536, ge=8)
    PASSWORD_ARGON2_PARALLELISM: int = Field(default=4, ge=1)

    # ------------------------------------------------------------- pricing
    # Order money is computed here and nowhere else. The Angular client
    # hardcodes DELIVERY_CHARGE = 99 and FREE_DELIVERY_THRESHOLD = 499; these are
    # the same policy, moved server-side so a client cannot choose its own
    # delivery fee and so it can be changed without a frontend deploy.
    DELIVERY_CHARGE: Decimal = Decimal("99.00")
    #: Subtotal at or above which delivery is free. Set to 0 to disable
    #: free delivery entirely (every order pays the delivery charge).
    FREE_DELIVERY_THRESHOLD: Decimal = Decimal("499.00")
    #: Upper bound on the total number of units in one order. Per-line
    #: quantity is already capped; this bounds a cart with many lines.
    MAX_ORDER_ITEMS: int = Field(default=200, ge=1, le=10_000)
    #: Upper bound on distinct lines in one order.
    MAX_ORDER_LINES: int = Field(default=50, ge=1, le=1_000)

    # ----------------------------------------------------------- razorpay
    # Optional: unset means online payments are disabled, not that they fail
    # silently. The key id is public; the two secrets are server-only.
    RAZORPAY_KEY_ID: SecretStr | None = None
    RAZORPAY_KEY_SECRET: SecretStr | None = None
    RAZORPAY_WEBHOOK_SECRET: SecretStr | None = None

    # --------------------------------------------------------------- smtp
    # Phase 7: settings for the real transport. Delivery is currently mocked in
    # ``services/email_service.py``; these are consumed once that is implemented.
    SMTP_HOST: str | None = None
    SMTP_PORT: int = Field(default=587, ge=1, le=65535)
    SMTP_USER: SecretStr | None = None
    SMTP_PASSWORD: SecretStr | None = None
    SMTP_FROM_EMAIL: str = "no-reply@bansikunj.in"
    SMTP_FROM_NAME: str = "Bansikunj"
    SMTP_USE_TLS: bool = True
    SMTP_USE_SSL: bool = False

    # --------------------------------------------------------- cloudinary
    # TODO Phase 3+: object storage for product imagery.
    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: SecretStr | None = None
    CLOUDINARY_API_SECRET: SecretStr | None = None
    CLOUDINARY_FOLDER: str = "bansikunj"

    # -------------------------------------------------------------- redis
    # TODO Phase 9: rate limiting and caching.
    REDIS_URL: SecretStr | None = None

    # ------------------------------------------------------------- cors
    # TODO Phase 9: assert no wildcard origin in production.
    FRONTEND_URL: str = "http://localhost:4200"
    CORS_ORIGINS: str = ""

    # ----------------------------------------------------------- logging
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "console"

    @field_validator("FRONTEND_URL")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _blank_database_url_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _require_database_credentials(self) -> "Settings":
        if self.DATABASE_URL is None and self.POSTGRES_PASSWORD is None:
            raise ValueError(
                "either DATABASE_URL or POSTGRES_PASSWORD must be set"
            )
        return self

    # ---------------------------------------------------------- accessors
    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def sqlalchemy_database_uri(self) -> str:
        """Async SQLAlchemy DSN.

        A plain property (deliberately not a ``computed_field``) so the value can
        never be picked up by ``model_dump()`` or an error serialiser.
        """
        if self.DATABASE_URL is not None:
            return _normalise_database_url(self.DATABASE_URL.get_secret_value())

        if self.POSTGRES_PASSWORD is None:
            raise RuntimeError(
                "DATABASE_URL is not set and POSTGRES_PASSWORD is missing"
            )
        password = self.POSTGRES_PASSWORD.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{password}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def cors_origins(self) -> list[str]:
        """Allow-list for CORS: the frontend origin plus any explicit extras."""
        origins = [self.FRONTEND_URL]
        origins.extend(
            origin.strip()
            for origin in self.CORS_ORIGINS.split(",")
            if origin.strip()
        )
        # Preserve order while removing duplicates.
        return list(dict.fromkeys(origins))

    def assert_production_safe(self) -> None:
        """Fail fast on configurations that are unsafe outside local development.

        Optional integrations (payments, email, media, Redis) may remain unset:
        their endpoints already fail closed with a 503, so a deployment can run
        without them while still being configured correctly.
        """
        if not self.is_production:
            return
        if self.DEBUG:
            raise RuntimeError("DEBUG must be false when ENVIRONMENT=production")
        if "*" in self.cors_origins:
            raise RuntimeError("CORS_ORIGINS must not contain '*' in production")

        optional = (
            "RAZORPAY_KEY_ID",
            "RAZORPAY_KEY_SECRET",
            "RAZORPAY_WEBHOOK_SECRET",
            "SMTP_HOST",
            "CLOUDINARY_CLOUD_NAME",
            "REDIS_URL",
        )
        disabled = [name for name in optional if getattr(self, name) is None]
        if disabled:
            get_logger(__name__).warning(
                "config.optional_integrations_disabled",
                integrations=disabled,
                message=(
                    "Related features will fail closed until these values are set."
                ),
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()

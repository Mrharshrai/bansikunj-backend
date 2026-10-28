"""Configuration tests that do not touch a database."""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings

JWT_SECRET = "x" * 48
NEON_URL = (
    "postgresql://bansikunj:p%40ss@ep-cool.ap-southeast-1.aws.neon.tech/"
    "neondb?sslmode=require&channel_binding=require"
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "JWT_SECRET_KEY": JWT_SECRET,
        "DATABASE_URL": NEON_URL,
    }
    values.update(overrides)
    return Settings(**values)


def test_database_url_alone_is_sufficient() -> None:
    settings = _settings()
    assert settings.POSTGRES_PASSWORD is None
    assert settings.sqlalchemy_database_uri.startswith(
        "postgresql+asyncpg://bansikunj:"
    )


def test_missing_database_credentials_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, JWT_SECRET_KEY=JWT_SECRET)


def test_neon_url_is_rewritten_for_asyncpg() -> None:
    url = make_url(_settings().sqlalchemy_database_uri)
    assert url.drivername == "postgresql+asyncpg"
    assert url.password == "p@ss"
    assert dict(url.query) == {"ssl": "require"}
    assert url.host == "ep-cool.ap-southeast-1.aws.neon.tech"


def test_bare_postgres_url_gets_asyncpg_driver() -> None:
    url = make_url(
        _settings(DATABASE_URL="postgres://user:pass@localhost:5432/bansikunj")
        .sqlalchemy_database_uri
    )
    assert url.drivername == "postgresql+asyncpg"
    assert dict(url.query) == {}


def test_production_allows_optional_integrations_to_be_unset() -> None:
    _settings(ENVIRONMENT="production").assert_production_safe()


def test_production_rejects_debug() -> None:
    with pytest.raises(RuntimeError, match="DEBUG"):
        _settings(ENVIRONMENT="production", DEBUG=True).assert_production_safe()


def test_production_rejects_wildcard_cors() -> None:
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        _settings(
            ENVIRONMENT="production", CORS_ORIGINS="*"
        ).assert_production_safe()

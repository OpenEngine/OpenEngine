"""The one setting that picks the database backend for every service.

`OE_DATABASE_BACKEND` is `sqlite` (the default) or `postgres`. SQLite keeps one
file per service under `OE_SQLITE_DIRECTORY`, which defaults to
`$XDG_STATE_HOME/openengine/databases`. Postgres connects to `OE_POSTGRES_URL`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

BACKEND_VARIABLE = "OE_DATABASE_BACKEND"
SQLITE_DIRECTORY_VARIABLE = "OE_SQLITE_DIRECTORY"
POSTGRES_URL_VARIABLE = "OE_POSTGRES_URL"
POSTGRES_MIGRATION_URL_VARIABLE = "OE_POSTGRES_MIGRATION_URL"


class Backend(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class ConfigError(ValueError):
    pass


class UnknownBackend(ConfigError):
    def __init__(self, name: str) -> None:
        choices = ", ".join(backend.value for backend in Backend)
        super().__init__(f"{BACKEND_VARIABLE}={name!r} is not one of {choices}")


class MissingPostgresURL(ConfigError):
    def __init__(self, variable: str) -> None:
        super().__init__(f"{variable} is required for this Postgres connection")


class InvalidPostgresURL(ConfigError):
    def __init__(self, variable: str) -> None:
        super().__init__(f"{variable} must be a PostgreSQL URL with user, host and database; "
                         "only TLS, timeout, application_name and target_session_attrs options are supported")


def postgres_url(value: str | None, variable: str) -> URL:
    """Validate explicit connection identity without exposing credentials in errors."""
    if not value:
        raise MissingPostgresURL(variable)
    try:
        url = make_url(value)
        valid = (
            url.drivername in {"postgresql", "postgresql+psycopg", "postgresql+psycopg_async"}
            and url.username and url.host and url.database
            and (url.port is None or 0 < url.port < 65536)
            # Identity and startup options must not override the URL or our search_path.
            and not set(url.query) - {
                "sslmode", "sslcert", "sslkey", "sslrootcert", "sslcrl",
                "connect_timeout", "application_name", "target_session_attrs",
            }
            and all(isinstance(v, str) for v in url.query.values())
        )
    except (ArgumentError, ValueError, TypeError):
        valid = False
    if not valid:
        raise InvalidPostgresURL(variable) from None
    return url


def default_sqlite_directory() -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local/state"
    return root / "openengine" / "databases"


@dataclass(frozen=True)
class DatabaseConfig:
    backend: Backend = Backend.SQLITE
    sqlite_directory: Path = field(default_factory=default_sqlite_directory)
    postgres_url: str | None = field(default=None, repr=False)
    postgres_migration_url: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.backend != Backend.POSTGRES:
            return
        for value, variable in ((self.postgres_url, POSTGRES_URL_VARIABLE),
                                (self.postgres_migration_url, POSTGRES_MIGRATION_URL_VARIABLE)):
            if value is not None:
                postgres_url(value, variable)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] = os.environ) -> DatabaseConfig:
        name = environment.get(BACKEND_VARIABLE) or Backend.SQLITE
        try:
            backend = Backend(name.lower())
        except ValueError:
            raise UnknownBackend(name) from None
        directory = environment.get(SQLITE_DIRECTORY_VARIABLE)
        return cls(
            backend=backend,
            sqlite_directory=Path(directory) if directory else default_sqlite_directory(),
            postgres_url=environment.get(POSTGRES_URL_VARIABLE) or None,
            postgres_migration_url=environment.get(POSTGRES_MIGRATION_URL_VARIABLE) or None,
        )

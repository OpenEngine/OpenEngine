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

BACKEND_VARIABLE = "OE_DATABASE_BACKEND"
SQLITE_DIRECTORY_VARIABLE = "OE_SQLITE_DIRECTORY"
POSTGRES_URL_VARIABLE = "OE_POSTGRES_URL"


class Backend(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


class ConfigError(ValueError):
    pass


class UnknownBackend(ConfigError):
    def __init__(self, name: str) -> None:
        choices = ", ".join(backend.value for backend in Backend)
        super().__init__(f"{BACKEND_VARIABLE}={name!r} is not one of {choices}")


def default_sqlite_directory() -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    root = Path(state) if state and Path(state).is_absolute() else Path.home() / ".local/state"
    return root / "openengine" / "databases"


@dataclass(frozen=True)
class DatabaseConfig:
    backend: Backend = Backend.SQLITE
    sqlite_directory: Path = field(default_factory=default_sqlite_directory)
    postgres_url: str | None = None

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
        )

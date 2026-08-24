"""Select and run the Alembic history for the active database."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import sqlite3
from typing import Literal

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool


DatabaseKind = Literal["sqlite", "postgres"]
_MIGRATIONS = Path(__file__).resolve().parent


def database_kind(database: str | Connection | sqlite3.Connection) -> DatabaseKind:
    """Return the migration history used by a URL or open connection."""
    if isinstance(database, sqlite3.Connection):
        return "sqlite"
    if isinstance(database, Connection):
        backend = database.dialect.name
    else:
        backend = make_url(database).get_backend_name()
    if backend == "sqlite":
        return "sqlite"
    if backend == "postgresql":
        return "postgres"
    raise ValueError(f"unsupported database backend: {backend}")


def alembic_config(database_url: str) -> Config:
    """Build an Alembic config pointed at the active database's history."""
    kind = database_kind(database_url)
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS / kind))
    # ConfigParser treats percent signs as interpolation, while passwords in a
    # valid database URL may contain percent escapes.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade(database_url: str, revision: str = "head") -> None:
    """Upgrade a database URL using its dialect-specific history."""
    command.upgrade(alembic_config(database_url), revision)


def upgrade_connection(
    connection: Connection | sqlite3.Connection, revision: str = "head"
) -> None:
    """Upgrade an existing connection, including an in-memory SQLite database."""
    kind = database_kind(connection)
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS / kind))

    if isinstance(connection, Connection):
        config.attributes["connection"] = connection
        command.upgrade(config, revision)
        return

    # Alembic operates on SQLAlchemy connections. StaticPool lets it borrow the
    # adapter's sqlite3 connection without closing or replacing an in-memory DB.
    engine = create_engine(
        "sqlite://",
        creator=lambda: connection,
        poolclass=StaticPool,
    )
    with engine.connect() as sqlalchemy_connection:
        config.attributes["connection"] = sqlalchemy_connection
        command.upgrade(config, revision)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database_url",
        nargs="?",
        default=os.environ.get("DATABASE_URL"),
        help="SQLAlchemy database URL (defaults to DATABASE_URL)",
    )
    parser.add_argument("--revision", default="head")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("database_url is required when DATABASE_URL is not set")
    upgrade(args.database_url, args.revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

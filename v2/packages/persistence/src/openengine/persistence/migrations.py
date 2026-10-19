"""Alembic, pointed at a service's persistence/migrations.

A service keeps one migration history, written with SQLAlchemy and Alembic's
portable operations, and it runs unchanged on every backend. Batch mode is on
so column changes work on SQLite; on Postgres the same batch operations run as
plain ALTERs.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from alembic import command, context
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import Script
from sqlalchemy import MetaData, create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.types import TypeDecorator

from openengine.persistence.database import Database

TEMPLATE = files("openengine.persistence") / "template"
TEMPLATE_FILES = ("env.py", "script.py.mako")


def scaffold(migrations: Path) -> None:
    """Give a migrations directory the shared Alembic environment, keeping any files already there."""
    (migrations / "versions").mkdir(parents=True, exist_ok=True)
    for name in TEMPLATE_FILES:
        target = migrations / name
        if not target.exists():
            target.write_text((TEMPLATE / name).read_text())


def alembic_config(migrations: Path, database: Database | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(migrations))
    if database is not None:
        config.attributes["database"] = database
        url = database.url(asynchronous=False).render_as_string(hide_password=False)
        # ConfigParser reads percent signs as interpolation, and passwords may contain escapes.
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@dataclass(frozen=True)
class Upgrade:
    """A database's revision before and after an upgrade; None before its first migration."""

    service: str
    before: str | None
    after: str | None


def _version_options(database: Database) -> dict[str, Any]:
    return {"version_table_schema": database.schema} if database.schema is not None else {}


def current_revision(database: Database) -> str | None:
    engine = database.migration_engine()
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection, opts=_version_options(database)).get_current_revision()
    finally:
        engine.dispose()


def upgrade(migrations: Path, database: Database, revision: str = "head") -> Upgrade:
    before = current_revision(database)
    command.upgrade(alembic_config(migrations, database), revision)
    return Upgrade(database.service, before, current_revision(database))


def downgrade(migrations: Path, database: Database, revision: str) -> None:
    command.downgrade(alembic_config(migrations, database), revision)


def revision(migrations: Path, message: str, *, database: Database | None = None, autogenerate: bool = False) -> Script:
    """Write a new revision; autogenerate compares the env's metadata against `database`."""
    script = command.revision(alembic_config(migrations, database), message=message, autogenerate=autogenerate)
    assert isinstance(script, Script)
    return script


def drift(database: Database, metadata: MetaData) -> list[Any]:
    """Alembic's diff between the database and `metadata`, ignoring its own version table."""
    engine = database.migration_engine()
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection, opts={"compare_type": True})
            return compare_metadata(context, metadata)
    finally:
        engine.dispose()


def render_item(kind: str, item: Any, autogen_context: Any) -> str | Literal[False]:
    """Write shared column types as the plain SQLAlchemy types they store, so revisions never import app code."""
    if kind == "type" and isinstance(item, TypeDecorator):
        return f"sa.{item.impl!r}"
    return False


def run_environment(target_metadata: MetaData | None = None) -> None:
    """The body of every service's env.py. Pass the service's metadata to autogenerate revisions."""
    config = context.config
    options = {"target_metadata": target_metadata, "render_as_batch": True, "render_item": render_item}
    if context.is_offline_mode():
        context.configure(
            url=config.get_main_option("sqlalchemy.url"),
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            **options,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    database: Database | None = config.attributes.get("database")
    if database is not None:
        options.update(_version_options(database))
        engine = database.migration_engine()
    else:
        # Run from the `alembic` command line with sqlalchemy.url set.
        engine = create_engine(config.get_main_option("sqlalchemy.url"), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, **options)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()

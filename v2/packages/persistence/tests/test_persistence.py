from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

import pytest
from sqlalchemy import inspect, text

from openengine.persistence import (
    LOCAL_TENANT,
    Backend,
    BackendNotImplemented,
    ConfigError,
    TenantDatabase,
    DatabaseConfig,
    ServicePersistence,
    open_database,
)
from openengine.persistence.migrations import revision, scaffold
from openengine.persistence.postgres import PostgresDatabase
from openengine.persistence.sqlite import SqliteDatabase

CREATE_JOBS = '''
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
    )'''


class Jobs(Protocol):
    async def add(self, name: str) -> None: ...
    async def names(self) -> list[str]: ...


class SqliteJobs:
    def __init__(self, database: TenantDatabase) -> None:
        self.database = database

    async def add(self, name: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(text("INSERT INTO jobs (name) VALUES (:name)"), {"name": name})

    async def names(self) -> list[str]:
        async with self.database.transaction() as connection:
            return list((await connection.execute(text("SELECT name FROM jobs ORDER BY id"))).scalars())


def jobs_service(tmp_path: Path) -> ServicePersistence[Jobs]:
    """A service whose persistence/ holds one migration creating `jobs`."""
    persistence = ServicePersistence[Jobs]("jobs", tmp_path / "persistence", {Backend.SQLITE: SqliteJobs})
    scaffold(persistence.migrations)
    script = revision(persistence.migrations, "create jobs")
    path = Path(script.path)
    path.write_text(path.read_text().replace("def upgrade() -> None:\n    pass", f"def upgrade() -> None:{CREATE_JOBS}"))
    return persistence


def test_defaults_to_sqlite_under_the_state_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    config = DatabaseConfig.from_environment({})

    assert config.backend is Backend.SQLITE
    assert config.sqlite_directory == tmp_path / "openengine" / "databases"


def test_one_variable_switches_every_service_to_postgres() -> None:
    config = DatabaseConfig.from_environment({"OE_DATABASE_BACKEND": "Postgres", "OE_POSTGRES_URL": "postgresql://runtime@db/oe"})

    assert isinstance(open_database("cxn", config), PostgresDatabase)
    assert isinstance(open_database("cxn", DatabaseConfig(sqlite_directory=Path("/x"))), SqliteDatabase)


@pytest.mark.parametrize("backend", [None, "sqlite"])
@pytest.mark.parametrize("variable", ["OE_POSTGRES_URL", "OE_POSTGRES_MIGRATION_URL"])
@pytest.mark.parametrize("value", ["not-a-url", "postgresql://u@db/oe?channel_binding=require"])
def test_sqlite_ignores_unused_postgres_urls(tmp_path, backend, variable, value):
    environment = {"OE_SQLITE_DIRECTORY": str(tmp_path), variable: value}
    if backend is not None:
        environment["OE_DATABASE_BACKEND"] = backend
    database = open_database("cxn", DatabaseConfig.from_environment(environment))
    assert isinstance(database, SqliteDatabase)

    async def query():
        try:
            async with database.transaction(LOCAL_TENANT) as connection:
                assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
        finally:
            await database.close()

    asyncio.run(query())


def test_refuses_an_unknown_backend() -> None:
    with pytest.raises(ConfigError, match="'mysql' is not one of sqlite, postgres"):
        DatabaseConfig.from_environment({"OE_DATABASE_BACKEND": "mysql"})


def test_postgres_requires_runtime_credentials_on_use() -> None:
    database = open_database("cxn", DatabaseConfig(backend=Backend.POSTGRES))

    with pytest.raises(ConfigError, match="OE_POSTGRES_URL is required"):
        database.engine


def test_reports_a_service_without_a_store_for_the_backend(tmp_path: Path) -> None:
    persistence = ServicePersistence[Jobs]("jobs", tmp_path, {Backend.SQLITE: SqliteJobs})

    with pytest.raises(BackendNotImplemented, match="jobs's store"):
        persistence.open(DatabaseConfig(backend=Backend.POSTGRES))


def test_scaffold_keeps_a_service_edited_env(tmp_path: Path) -> None:
    (tmp_path / "env.py").write_text("# edited")

    scaffold(tmp_path)

    assert (tmp_path / "env.py").read_text() == "# edited"
    assert (tmp_path / "script.py.mako").exists()
    assert (tmp_path / "versions").is_dir()


def test_migrates_and_serves_a_service_on_sqlite(tmp_path: Path) -> None:
    persistence = jobs_service(tmp_path)
    config = DatabaseConfig(sqlite_directory=tmp_path / "databases")

    persistence.upgrade(config)
    persistence.upgrade(config)  # already at head

    async def round_trip() -> list[str]:
        jobs = persistence.open(config).scoped(LOCAL_TENANT)
        try:
            await jobs.add("first")
            await jobs.add("second")
            return await jobs.names()
        finally:
            await jobs.database.close()

    assert asyncio.run(round_trip()) == ["first", "second"]
    engine = persistence.database(config).migration_engine()
    assert {"jobs", "alembic_version"} <= set(inspect(engine).get_table_names())


def test_sqlite_connections_enforce_foreign_keys_and_use_wal(tmp_path: Path) -> None:
    database = open_database("cxn", DatabaseConfig(sqlite_directory=tmp_path))

    async def pragmas() -> tuple[int, str]:
        async with database.connect() as connection:
            foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar_one()
            journal = (await connection.execute(text("PRAGMA journal_mode"))).scalar_one()
        await database.close()
        return foreign_keys, journal

    assert asyncio.run(pragmas()) == (1, "wal")
    assert (tmp_path / "cxn.sqlite3").exists()


def test_cold_connection_waits_for_journal_mode_lock_without_blocking_the_loop(tmp_path: Path) -> None:
    import sqlite3

    database = open_database("cold", DatabaseConfig(sqlite_directory=tmp_path))
    # A rollback-journal reader prevents the initial transition to WAL. This is
    # connection setup contention, not application BEGIN IMMEDIATE contention.
    blocker = sqlite3.connect(tmp_path / "cold.sqlite3")
    blocker.execute("CREATE TABLE existing (id INTEGER)")
    blocker.execute("BEGIN")
    blocker.execute("SELECT * FROM existing").fetchall()

    async def scenario() -> None:
        async def release() -> None:
            await asyncio.sleep(0.05)
            blocker.rollback()

        release_task = asyncio.create_task(release())
        try:
            async with database.connect() as connection:
                assert (await connection.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"
                assert (await connection.execute(text("PRAGMA busy_timeout"))).scalar_one() == 5000
            await release_task
        finally:
            await release_task
            await database.close()

    try:
        asyncio.run(scenario())
    finally:
        blocker.close()


@pytest.mark.parametrize("value", ["not-a-url", "sqlite:///db", "postgresql://db/oe", "postgresql://u@db",
                                   "postgresql://u@db:bad/oe", "postgresql://u@db/oe?user=owner",
                                   "postgresql://u@db/oe?options=-csearch_path=public",
                                   "postgresql://u@db/oe?channel_binding=require"])
def test_postgres_configuration_rejects_invalid_urls_without_exposing_secrets(value):
    for variable in ("OE_POSTGRES_URL", "OE_POSTGRES_MIGRATION_URL"):
        with pytest.raises(ConfigError) as error:
            DatabaseConfig.from_environment({"OE_DATABASE_BACKEND": "postgres", variable: value})
        assert variable in str(error.value)
        assert value not in str(error.value)


def test_postgres_urls_never_fall_back_between_runtime_and_owner():
    environment = {
        "OE_DATABASE_BACKEND": "postgres",
        "OE_POSTGRES_URL": "postgresql://runtime:p%40ss@db/oe?sslmode=require",
        "OE_POSTGRES_MIGRATION_URL": "postgresql://owner:secret@db/oe",
    }
    config = DatabaseConfig.from_environment(environment)
    database = open_database("cxn", config)
    runtime, owner = database.url(), database.url(asynchronous=False)
    assert (runtime.drivername, runtime.username, runtime.password) == ("postgresql+psycopg_async", "runtime", "p@ss")
    assert (owner.drivername, owner.username) == ("postgresql+psycopg", "owner")
    assert runtime.query["sslmode"] == "require"
    assert runtime.query["options"] == owner.query["options"] == '-csearch_path="cxn"'
    assert "secret" not in repr(config) and "p%40ss" not in repr(config)
    for missing, asynchronous in (("OE_POSTGRES_URL", True), ("OE_POSTGRES_MIGRATION_URL", False)):
        partial = DatabaseConfig.from_environment({key: value for key, value in environment.items() if key != missing})
        with pytest.raises(ConfigError, match=missing):
            open_database("cxn", partial).url(asynchronous=asynchronous)


@pytest.mark.parametrize("service", ["public", "pg_catalog", "cxn,public", 'cxn"; SELECT 1', "a" * 64])
def test_postgres_schema_names_are_validated(service):
    with pytest.raises(ConfigError, match="schema"):
        open_database(service, DatabaseConfig(backend=Backend.POSTGRES))


def test_postgres_refuses_versions_before_16(monkeypatch):
    from unittest.mock import MagicMock

    from sqlalchemy import event

    listeners = []
    monkeypatch.setattr(event, "listens_for", lambda engine, name: listeners.append)
    database = PostgresDatabase("cxn", "postgresql://runtime@db/oe")
    database.configure(MagicMock())
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.fetchone.return_value = (150099, "cxn")
    with pytest.raises(ConfigError, match="16 or newer"):
        listeners[0](connection, None)
    connection.rollback.assert_called_once()

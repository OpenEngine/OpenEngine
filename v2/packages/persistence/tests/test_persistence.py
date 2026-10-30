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
    Database,
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
    def __init__(self, database: Database) -> None:
        self.database = database

    async def add(self, name: str) -> None:
        async with self.database.transaction() as connection:
            await connection.execute(text("INSERT INTO jobs (name) VALUES (:name)"), {"name": name})

    async def names(self) -> list[str]:
        async with self.database.connect() as connection:
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
    config = DatabaseConfig.from_environment({"OE_DATABASE_BACKEND": "Postgres", "OE_POSTGRES_URL": "postgresql://db/oe"})

    assert isinstance(open_database("cxn", config), PostgresDatabase)
    assert isinstance(open_database("cxn", DatabaseConfig(sqlite_directory=Path("/x"))), SqliteDatabase)


def test_refuses_an_unknown_backend() -> None:
    with pytest.raises(ConfigError, match="'mysql' is not one of sqlite, postgres"):
        DatabaseConfig.from_environment({"OE_DATABASE_BACKEND": "mysql"})


def test_postgres_is_a_stub_that_fails_on_use() -> None:
    database = open_database("cxn", DatabaseConfig(backend=Backend.POSTGRES))

    with pytest.raises(BackendNotImplemented, match="postgres backend does not implement connections"):
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

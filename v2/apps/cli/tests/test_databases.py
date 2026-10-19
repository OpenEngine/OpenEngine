from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from openengine.cli.migrations.databases import (
    InvalidDatabaseConfig,
    DatabaseMigrationFailed,
    Outcome,
    database_config,
    migrate_databases,
    outcome,
)
from openengine.persistence import Backend, DatabaseConfig, open_database
from openengine.persistence.migrations import Upgrade


def test_creates_each_services_database_then_leaves_it_at_head(tmp_path: Path) -> None:
    config = DatabaseConfig(sqlite_directory=tmp_path)

    first = migrate_databases(config)
    again = migrate_databases(config)

    assert [(upgrade.service, outcome(upgrade)) for upgrade in first] == [
        ("cxn", Outcome.CREATED),
        ("ge", Outcome.CREATED),
        ("vcp", Outcome.CREATED),
    ]
    assert {outcome(upgrade) for upgrade in again} == {Outcome.UNCHANGED}
    assert [upgrade.after for upgrade in again] == [upgrade.after for upgrade in first]
    tables = inspect(open_database("ge", config).migration_engine()).get_table_names()
    assert "graphs" in tables


def test_names_an_upgrade_from_an_earlier_revision() -> None:
    assert outcome(Upgrade("ge", "a1", "b2")) is Outcome.UPGRADED


def test_reports_the_postgres_stub_instead_of_a_traceback() -> None:
    with pytest.raises(DatabaseMigrationFailed, match="could not migrate the cxn database: the postgres backend"):
        migrate_databases(DatabaseConfig(backend=Backend.POSTGRES))


def test_reports_an_unknown_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_DATABASE_BACKEND", "mysql")

    with pytest.raises(InvalidDatabaseConfig, match="'mysql' is not one of sqlite, postgres"):
        database_config()

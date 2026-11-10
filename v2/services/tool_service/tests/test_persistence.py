from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect

from openengine.persistence import Backend, BackendNotImplemented, DatabaseConfig
from openengine.persistence.migrations import downgrade

from tool_service.persistence import PERSISTENCE
from tool_service.persistence.schema import metadata


@pytest.fixture
def config(tmp_path: Path) -> DatabaseConfig:
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return config


def test_migrations_match_the_schema(config: DatabaseConfig) -> None:
    assert PERSISTENCE.drift(metadata, config) == []


def test_migrations_create_the_tables_in_three_revisions(config: DatabaseConfig) -> None:
    database = PERSISTENCE.database(config)
    engine = database.migration_engine()
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version", "tools", "tool_grants", "tool_invocations", "tool_invocation_attempts", "policies",
    }
    grant_columns = {c["name"] for c in inspect(engine).get_columns("tool_grants")}
    assert "when" in grant_columns and "constraints" not in grant_columns
    downgrade(PERSISTENCE.migrations, database, "-1")
    assert set(inspect(engine).get_table_names()) == {
        "alembic_version", "tools", "tool_grants", "tool_invocations", "tool_invocation_attempts",
    }


def test_migrations_roll_back(config: DatabaseConfig) -> None:
    database = PERSISTENCE.database(config)
    downgrade(PERSISTENCE.migrations, database, "base")

    assert inspect(database.migration_engine()).get_table_names() == ["alembic_version"]


def test_resources_are_unique_by_namespace_and_name(config: DatabaseConfig) -> None:
    inspector = inspect(PERSISTENCE.database(config).migration_engine())
    for table in ("tools", "tool_grants", "tool_invocations", "policies"):
        assert [c["column_names"] for c in inspector.get_unique_constraints(table)] == [["namespace", "name"]], table
        assert inspector.get_pk_constraint(table)["constrained_columns"] == ["uid"], table


def test_there_is_no_store_yet() -> None:
    for backend in (Backend.SQLITE, Backend.POSTGRES):
        with pytest.raises(BackendNotImplemented, match="tools's store"):
            PERSISTENCE.open(DatabaseConfig(backend=backend))

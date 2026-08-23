"""Alembic selects and applies the database-specific migration history."""

from pathlib import Path
import sqlite3

from alembic import command
import pytest

from migrations.migration import alembic_config, database_kind, upgrade


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:///state.sqlite3", "sqlite"),
        ("sqlite+pysqlite:///:memory:", "sqlite"),
        ("postgresql://localhost/engine", "postgres"),
        ("postgresql+psycopg://localhost/engine", "postgres"),
    ],
)
def test_database_kind_selects_a_dialect_specific_history(
    url: str, expected: str
) -> None:
    assert database_kind(url) == expected
    assert Path(alembic_config(url).get_main_option("script_location")).name == expected


def test_database_kind_rejects_unsupported_databases() -> None:
    with pytest.raises(ValueError, match="unsupported database backend: mysql"):
        database_kind("mysql://localhost/engine")


def test_sqlite_upgrade_creates_and_stamps_the_schema(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"

    upgrade(f"sqlite:///{database}")

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()

    assert {"agent_instances", "projects", "session_grants"} <= tables
    assert revision == ("sqlite_0001",)


def test_postgres_history_can_render_without_a_live_database(capsys) -> None:
    config = alembic_config("postgresql+psycopg://localhost/engine")

    command.upgrade(config, "head", sql=True)

    sql = capsys.readouterr().out
    assert "CREATE TABLE agent_instances" in sql
    assert "postgres_0001" in sql

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


def test_upgrade_rejects_postgres_alias_before_running_alembic() -> None:
    with pytest.raises(ValueError, match="unsupported database backend: postgres"):
        upgrade("postgres://localhost/engine")


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
    assert revision == ("sqlite_0003",)


def test_milestone_details_migration_preserves_existing_records(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    url = f"sqlite:///{database}"
    upgrade(url, "sqlite_0001")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO projects (project_id, name) VALUES (?, ?)",
            ("project-engine", "OpenEngine"),
        )
        connection.execute(
            """
            INSERT INTO milestones (milestone_id, project_id, name)
            VALUES (?, ?, ?)
            """,
            ("milestone-foundation", "project-engine", "Foundation"),
        )
        connection.commit()

    upgrade(url)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """
            SELECT name, description, dependencies FROM milestones
            WHERE milestone_id = ?
            """,
            ("milestone-foundation",),
        ).fetchone()
    assert row == ("Foundation", "", "[]")


def test_project_archive_migration_leaves_existing_projects_listed(
    tmp_path: Path,
) -> None:
    """Archiving is new, so nothing recorded before it is put away by it."""

    database = tmp_path / "state.sqlite3"
    url = f"sqlite:///{database}"
    upgrade(url, "sqlite_0002")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO projects (project_id, name) VALUES (?, ?)",
            ("project-engine", "OpenEngine"),
        )
        connection.commit()

    upgrade(url)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT name, archived FROM projects WHERE project_id = ?",
            ("project-engine",),
        ).fetchone()
    assert row == ("OpenEngine", 0)


def test_postgres_history_is_a_placeholder(capsys) -> None:
    config = alembic_config("postgresql+psycopg://localhost/engine")

    command.upgrade(config, "head", sql=True)

    sql = capsys.readouterr().out
    assert "CREATE TABLE agent_instances" not in sql
    assert "postgres_0001" in sql

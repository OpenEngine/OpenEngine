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
    assert revision == ("sqlite_0006",)


def test_message_conversation_index_is_used_after_upgrade(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    url = f"sqlite:///{database}"
    upgrade(url, "sqlite_0005")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO agent_instances (
                instance_id, agent_id, conversation_id, title, archived,
                runner, auto_approve
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("instance-1", "coder", "conversation-1", "Chat", 0, "codex", 0),
        )
        connection.execute(
            """
            INSERT INTO messages (instance_id, role, content, tool_calls)
            VALUES (?, ?, ?, ?)
            """,
            ("instance-1", "user", "Keep me", "[]"),
        )
        connection.commit()

    upgrade(url)

    with sqlite3.connect(database) as connection:
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT sequence, role, content, tool_calls, tool_call_id
            FROM messages WHERE instance_id = ? ORDER BY sequence
            """,
            ("instance-1",),
        ).fetchall()
        content = connection.execute(
            "SELECT content FROM messages WHERE instance_id = ?",
            ("instance-1",),
        ).fetchone()

    assert any("messages_by_instance" in row[3] for row in plan)
    assert content == ("Keep me",)


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


def test_workstream_scope_migration_leaves_existing_workstreams_named(
    tmp_path: Path,
) -> None:
    """Scope is new, so what was recorded without one reads back unscoped."""

    database = tmp_path / "state.sqlite3"
    url = f"sqlite:///{database}"
    upgrade(url, "sqlite_0003")
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
        connection.execute(
            """
            INSERT INTO workstreams (workstream_id, milestone_id, name)
            VALUES (?, ?, ?)
            """,
            ("workstream-data", "milestone-foundation", "Data model"),
        )
        connection.commit()

    upgrade(url)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT name, scope FROM workstreams WHERE workstream_id = ?",
            ("workstream-data",),
        ).fetchone()
    assert row == ("Data model", "")


def test_postgres_history_is_a_placeholder(capsys) -> None:
    config = alembic_config("postgresql+psycopg://localhost/engine")

    command.upgrade(config, "head", sql=True)

    sql = capsys.readouterr().out
    assert "CREATE TABLE agent_instances" not in sql
    assert "postgres_0001" in sql

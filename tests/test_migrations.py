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


def test_graph_migration_creates_an_independent_schema_and_downgrades(tmp_path: Path) -> None:
    from migrations.migration import main

    database = tmp_path / "graph.sqlite3"
    url = f"sqlite:///{database}"
    assert main([url, "--store", "graph"]) == 0
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )}
        assert tables == {"events", "runs", "sessions", "approvals", "github_comments", "sqlite_sequence", "alembic_version"}
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("c7c9f42f4747",)
        connection.execute("INSERT INTO runs (run_id, graph_id) VALUES ('run', 'graph')")
        assert connection.execute("SELECT auto_approve_nodes FROM runs").fetchone() == ("[]",)
        for table, index in (("events", "events_by_run"), ("approvals", "approvals_by_run")):
            assert index in {row[1] for row in connection.execute(f"PRAGMA index_list({table})")}
    command.downgrade(alembic_config(url, store="graph"), "base")
    upgrade(url, store="graph")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone() == (0,)


@pytest.mark.parametrize("has_auto_approve", [False, True])
def test_graph_migration_adopts_existing_data(tmp_path: Path, has_auto_approve: bool) -> None:
    from engine.domain import RunId
    from engine.graph_runtime import EventKind, RuntimeEvent
    from engine.graph_runtime_langgraph.store import SqliteGraphRuntimeStore

    database = tmp_path / "graph.sqlite3"
    with sqlite3.connect(database) as connection:
        # The schema shipped before Alembic, including an event deleted after
        # allocation: adoption must retain sqlite_sequence as well as live rows.
        connection.executescript("""
            CREATE TABLE events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, node_id TEXT, execution_id TEXT
            );
            CREATE INDEX events_by_run ON events (run_id, sequence);
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY, graph_id TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', ordinal INTEGER
            );
            CREATE TABLE sessions (
                run_id TEXT NOT NULL, session_key TEXT NOT NULL, continuation TEXT NOT NULL,
                PRIMARY KEY (run_id, session_key)
            );
            CREATE TABLE approvals (
                approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, record TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', decision TEXT, ordinal INTEGER
            );
            CREATE INDEX approvals_by_run ON approvals (run_id);
            INSERT INTO runs VALUES ('run', 'graph', '', 1);
            INSERT INTO sessions VALUES ('run', 'session', '{"session_id":"saved"}');
            INSERT INTO approvals VALUES ('approval', 'run', '{}', 'pending', NULL, 1);
        """)
        if has_auto_approve:
            connection.execute("ALTER TABLE runs ADD COLUMN auto_approve_nodes TEXT NOT NULL DEFAULT '[]'")
            connection.execute("UPDATE runs SET auto_approve_nodes = '[\"coder\"]'")
        connection.execute(
            "INSERT INTO events VALUES (7, 'run', ?, '{}', 'node', 'execution')",
            (EventKind.RUN_STARTED.value,),
        )
        connection.execute("INSERT INTO events (sequence, run_id, kind, payload) VALUES (8, 'run', 'unused', '{}')")
        connection.execute("DELETE FROM events WHERE sequence = 8")

    for _ in range(2):
        store = SqliteGraphRuntimeStore(database)
        events = store.events_since(RunId("run"))
        assert len(events) == 1
        assert (events[0].sequence, events[0].node_id, events[0].execution_id) == (7, "node", "execution")
        store.close()
    store = SqliteGraphRuntimeStore(database)
    assert store.append_event(RuntimeEvent(RunId("run"), EventKind.RUN_STARTED)).sequence == 9
    store.close()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM sessions").fetchone() == ("run", "session", '{"session_id":"saved"}')
        assert connection.execute("SELECT * FROM approvals").fetchone() == ("approval", "run", "{}", "pending", None, 1)
        assert connection.execute("SELECT graph_id, auto_approve_nodes FROM runs").fetchone() == (
            "graph", '["coder"]' if has_auto_approve else "[]"
        )
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("c7c9f42f4747",)


def test_github_comments_migration_preserves_graph_data_and_downgrades(
    tmp_path: Path,
) -> None:
    database = tmp_path / "graph.sqlite3"
    url = f"sqlite:///{database}"
    upgrade(url, "26a6a404b5fe", store="graph")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO runs (run_id, graph_id) VALUES ('run', 'graph')")
        connection.execute(
            "INSERT INTO events (run_id, kind, payload) VALUES ('run', 'started', '{}')"
        )
        events = connection.execute("SELECT * FROM events").fetchall()
        event_columns = connection.execute("PRAGMA table_info(events)").fetchall()

    upgrade(url, store="graph")
    upgrade(url, store="graph")

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO github_comments
                (comment_id, pr_number, run_id, node_id, posted_at, url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (12345, 42, "run", "review", "2026-09-10T18:00:00Z", "https://example.com/comment"),
        )
        connection.execute(
            """
            INSERT INTO github_comments (comment_id, pr_number, run_id, posted_at)
            VALUES (12346, 42, 'run', '2026-09-10T18:01:00Z')
            """
        )
        assert connection.execute(
            "SELECT node_id, url FROM github_comments WHERE comment_id = 12346"
        ).fetchone() == (None, None)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO github_comments SELECT * FROM github_comments WHERE comment_id = 12345"
            )
        for column in ("pr_number", "run_id", "posted_at"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE github_comments SET {column} = NULL WHERE comment_id = 12345"
                )
        for column, value, index in (
            ("run_id", "run", "github_comments_by_run"),
            ("pr_number", 42, "github_comments_by_pr"),
        ):
            plan = connection.execute(
                f"EXPLAIN QUERY PLAN SELECT * FROM github_comments WHERE {column} = ?",
                (value,),
            ).fetchall()
            assert any(index in row[3] for row in plan)
        assert connection.execute("SELECT * FROM events").fetchall() == events
        assert connection.execute("PRAGMA table_info(events)").fetchall() == event_columns

    command.downgrade(alembic_config(url, store="graph"), "26a6a404b5fe")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'github_comments%'"
        ).fetchall() == []
        assert connection.execute("SELECT run_id, graph_id FROM runs").fetchall() == [("run", "graph")]
        assert connection.execute("SELECT * FROM events").fetchall() == events

    upgrade(url, store="graph")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM github_comments").fetchone() == (0,)


def test_graph_history_rejects_postgres() -> None:
    with pytest.raises(ValueError, match="unsupported migration store/backend: graph/postgres"):
        alembic_config("postgresql://localhost/engine", store="graph")

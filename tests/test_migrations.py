"""Alembic selects and applies the database-specific migration history."""

import json
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
    assert revision == ("sqlite_0008",)


def test_sqlite_upgrade_removes_runs_with_retired_human_review_phase(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.sqlite3"
    url = f"sqlite:///{database}"
    upgrade(url, "sqlite_0006")
    with sqlite3.connect(database) as connection:
        connection.executemany(
            "INSERT INTO run_states (run_id, state_json) VALUES (?, ?)",
            (
                (
                    "run-current",
                    '{"run_id":"run-current","phase":"running_agent"}',
                ),
                (
                    "run-retired",
                    '{"run_id":"run-retired","phase":"awaiting_human_review"}',
                ),
                ("run-malformed", "not json"),
            ),
        )
        connection.commit()

    upgrade(url)

    with sqlite3.connect(database) as connection:
        runs = connection.execute(
            "SELECT run_id FROM run_states ORDER BY sequence"
        ).fetchall()

    assert runs == [("run-current",), ("run-malformed",)]


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


def test_removing_workstreams_rehomes_their_runs_on_the_milestone(
    tmp_path: Path,
) -> None:
    """A run outlives the heading it hung from, under the goal that heading served."""

    database = tmp_path / "state.sqlite3"
    url = f"sqlite:///{database}"
    upgrade(url, "sqlite_0007")
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
            INSERT INTO workstreams (workstream_id, milestone_id, name, scope)
            VALUES (?, ?, ?, ?)
            """,
            ("workstream-data", "milestone-foundation", "Data model", ""),
        )
        connection.executemany(
            """
            INSERT INTO run_states (run_id, state_json, workstream_id, milestone_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                (
                    "run-scoped",
                    '{"run_id":"run-scoped","workstream_id":"workstream-data",'
                    '"milestone_id":null}',
                    "workstream-data",
                    None,
                ),
                (
                    "run-loose",
                    '{"run_id":"run-loose","workstream_id":null,"milestone_id":null}',
                    None,
                    None,
                ),
            ),
        )
        connection.commit()

    upgrade(url)

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        rows = dict(
            connection.execute("SELECT run_id, milestone_id FROM run_states")
        )
        states = dict(connection.execute("SELECT run_id, state_json FROM run_states"))

    assert "workstreams" not in tables
    assert rows == {"run-scoped": "milestone-foundation", "run-loose": None}
    assert json.loads(states["run-scoped"]) == {
        "run_id": "run-scoped",
        "milestone_id": "milestone-foundation",
    }
    assert json.loads(states["run-loose"]) == {
        "run_id": "run-loose",
        "milestone_id": None,
    }


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
        assert tables == {"events", "runs", "sessions", "approvals", "github_comments", "github_pull_requests", "sqlite_sequence", "alembic_version"}
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("d3f81a6c2e90",)
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
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("d3f81a6c2e90",)


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
                (comment_id, repository, kind, pr_number, run_id, node_id, posted_at, url)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (12345, "acme/api", "issue", 42, "run", "review", "2026-09-10T18:00:00Z", "https://example.com/comment"),
        )
        connection.execute(
            """
            INSERT INTO github_comments (comment_id, repository, kind, pr_number, run_id, posted_at)
            VALUES (12346, 'acme/api', 'issue', 42, 'run', '2026-09-10T18:01:00Z')
            """
        )
        assert connection.execute(
            "SELECT node_id, url FROM github_comments WHERE comment_id = 12346"
        ).fetchone() == (None, None)
        # One id in two id spaces, and in two repositories, is three comments.
        connection.execute(
            """
            INSERT INTO github_comments (comment_id, repository, kind, pr_number, run_id, posted_at)
            VALUES (12345, 'acme/api', 'review', 42, 'run', '2026-09-10T18:02:00Z'),
                   (12345, 'acme/web', 'issue', 42, 'run', '2026-09-10T18:03:00Z')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO github_comments SELECT * FROM github_comments "
                "WHERE comment_id = 12345 AND repository = 'acme/api' AND kind = 'issue'"
            )
        for column in ("repository", "kind", "pr_number", "run_id", "posted_at"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE github_comments SET {column} = NULL WHERE comment_id = 12346"
                )
        for where, values, index in (
            ("run_id = ?", ("run",), "github_comments_by_run"),
            ("repository = ? AND pr_number = ?", ("acme/api", 42), "github_comments_by_pr"),
        ):
            plan = connection.execute(
                f"EXPLAIN QUERY PLAN SELECT * FROM github_comments WHERE {where}",
                values,
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


def test_github_pull_requests_migration_names_one_owner_and_downgrades(
    tmp_path: Path,
) -> None:
    """Ownership is keyed by the pull request, so it has exactly one holder."""
    database = tmp_path / "graph.sqlite3"
    url = f"sqlite:///{database}"
    upgrade(url, "b41d9c0f5a3e", store="graph")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO runs (run_id, graph_id) VALUES ('run', 'graph')")
        connection.execute(
            """
            INSERT INTO github_comments (comment_id, repository, kind, pr_number, run_id, posted_at)
            VALUES (12345, 'acme/api', 'issue', 42, 'run', '2026-09-10T18:00:00Z')
            """
        )
        comments = connection.execute("SELECT * FROM github_comments").fetchall()

    upgrade(url, store="graph")
    upgrade(url, store="graph")

    with sqlite3.connect(database) as connection:
        # Nothing is backfilled: the comment table cannot say who opened what.
        assert connection.execute(
            "SELECT COUNT(*) FROM github_pull_requests"
        ).fetchone() == (0,)
        assert connection.execute("SELECT * FROM github_comments").fetchall() == comments
        connection.execute(
            """
            INSERT INTO github_pull_requests
                (repository, number, run_id, node_id, opened_at, url)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("acme/api", 42, "run", "coder", "2026-09-10T17:00:00Z", "https://example.com/pull/42"),
        )
        connection.execute(
            """
            INSERT INTO github_pull_requests (repository, number, run_id, opened_at)
            VALUES ('acme/web', 42, 'run', '2026-09-10T17:01:00Z')
            """
        )
        assert connection.execute(
            "SELECT node_id, url FROM github_pull_requests WHERE repository = 'acme/web'"
        ).fetchone() == (None, None)
        # One repository's #42 is not another's, but its own #42 is itself.
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO github_pull_requests (repository, number, run_id, opened_at)
                VALUES ('acme/api', 42, 'other', '2026-09-10T17:02:00Z')
                """
            )
        for column in ("run_id", "opened_at"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"UPDATE github_pull_requests SET {column} = NULL "
                    "WHERE repository = 'acme/web'"
                )
        # The webhook's question is a primary-key seek, and the run's is indexed.
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM github_pull_requests "
            "WHERE repository = ? AND number = ?",
            ("acme/api", 42),
        ).fetchall()
        # SQLite serves a composite primary key from its own autoindex, so the
        # plan names that rather than the key; either way it is one seek.
        assert any(
            "sqlite_autoindex_github_pull_requests" in row[3] for row in plan
        ), plan
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM github_pull_requests WHERE run_id = ?",
            ("run",),
        ).fetchall()
        assert any("github_pull_requests_by_run" in row[3] for row in plan)

    command.downgrade(alembic_config(url, store="graph"), "b41d9c0f5a3e")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'github_pull_requests%'"
        ).fetchall() == []
        # The comments a run left outlive a rolled-back ownership table.
        assert connection.execute("SELECT * FROM github_comments").fetchall() == comments

    upgrade(url, store="graph")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM github_pull_requests"
        ).fetchone() == (0,)


def test_graph_history_rejects_postgres() -> None:
    with pytest.raises(ValueError, match="unsupported migration store/backend: graph/postgres"):
        alembic_config("postgresql://localhost/engine", store="graph")

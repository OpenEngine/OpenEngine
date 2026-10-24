"""The service's own rows, in the graph database beside the runs they describe.

Synchronous SQLite behind a small class, like `SqliteGraphRuntimeStore`: every
statement is short, and the tables are created by the graph store's Alembic
history (`migrations/sqlite_graph`) rather than here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from migrations.migration import upgrade_connection

#: Node execution statuses that will not change again.
TERMINAL_EXECUTION_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
OPEN_EXECUTION_STATUSES = frozenset({"running", "awaiting_approval"})


def now_iso(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class GraphRow:
    graph_id: str
    project: str
    name: str
    latest_version_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AgentRow:
    name: str
    kind: str
    model: str
    url: str
    created_at: str
    updated_at: str
    context_limit: int = 0
    output_limit: int = 0


@dataclass(frozen=True)
class VersionRow:
    version_id: str
    graph_id: str
    number: int
    manifest: dict[str, Any]
    digest: str
    created_at: str
    format: str = "yaml"
    source: str = ""


@dataclass(frozen=True)
class SubmissionRow:
    idempotency_key: str
    request_digest: str
    run_id: str
    version_id: str
    created_at: str


@dataclass(frozen=True)
class StartedRunRow:
    run_id: str
    version_id: str | None
    started_at: str | None
    loop_id: str | None


@dataclass(frozen=True)
class LoopRow:
    loop_id: str
    project: str
    name: str
    graph_id: str
    version_id: str
    instruction: str
    repository: str
    inputs: dict[str, str]
    interval_seconds: int
    max_prs: int | None
    max_spend_usd: float | None
    state: str
    pause_reason: str
    next_run_at: str
    active_run_id: str | None
    created_at: str
    updated_at: str
    last_error: str = ""
    last_error_at: str | None = None
    consecutive_start_failures: int = 0


@dataclass(frozen=True)
class ExecutionRow:
    execution_id: str
    run_id: str
    node_id: str
    attempt: int
    status: str
    started_at: str
    finished_at: str | None
    error: str


@dataclass(frozen=True)
class SteeringRow:
    sequence: int
    steering_id: str
    idempotency_key: str
    run_id: str
    execution_id: str
    message: str
    status: str
    error: str
    accepted_at: str
    delivered_at: str | None
    applied_at: str | None


class GraphServiceStore:
    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        # The runtime's store holds a second connection to this file.
        self._connection.execute("PRAGMA busy_timeout = 5000")
        upgrade_connection(self._connection, store="graph")

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A write lock taken up front, so a check and its insert cannot interleave."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")

    def verbs(self, project: str) -> dict[str, str]:
        row = self._connection.execute("SELECT verbs FROM cli_projects WHERE name = ?", (project,)).fetchone()
        return {"go": "implementation-review-rerank", "review": "review", "fix": "fix", **(json.loads(row[0]) if row else {})}

    def bind_verb(self, project: str, verb: str, graph: str) -> dict[str, str]:
        with self.transaction():
            verbs = {**self.verbs(project), verb: graph}
            self._connection.execute(
                "INSERT INTO cli_projects(name, verbs) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET verbs=excluded.verbs",
                (project, json.dumps(verbs)),
            )
        return verbs

    # --- agents -------------------------------------------------------------

    def agents(self) -> tuple[AgentRow, ...]:
        rows = self._connection.execute("SELECT * FROM cli_agents ORDER BY name")
        return tuple(AgentRow(**dict(row)) for row in rows)

    def agent(self, name: str) -> AgentRow | None:
        row = self._connection.execute("SELECT * FROM cli_agents WHERE name = ?", (name,)).fetchone()
        return AgentRow(**dict(row)) if row else None

    def upsert_agent(self, row: AgentRow) -> None:
        """Insert `row`, or replace the agent of that name and keep when it was added."""
        self._connection.execute(
            "INSERT INTO cli_agents (name, kind, model, url, created_at, updated_at, context_limit, output_limit) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
            "kind = excluded.kind, model = excluded.model, url = excluded.url, updated_at = excluded.updated_at, "
            "context_limit = excluded.context_limit, output_limit = excluded.output_limit",
            (
                row.name, row.kind, row.model, row.url, row.created_at, row.updated_at,
                row.context_limit, row.output_limit,
            ),
        )

    def delete_agent(self, name: str) -> None:
        self._connection.execute("DELETE FROM cli_agents WHERE name = ?", (name,))

    # --- graphs -------------------------------------------------------------

    def graphs(self, project: str | None = None) -> tuple[GraphRow, ...]:
        if project is None:
            rows = self._connection.execute("SELECT * FROM cli_graphs ORDER BY project, name")
        else:
            rows = self._connection.execute(
                "SELECT * FROM cli_graphs WHERE project = ? ORDER BY name", (project,)
            )
        return tuple(GraphRow(**dict(row)) for row in rows)

    def graph(self, graph_id: str) -> GraphRow | None:
        row = self._connection.execute(
            "SELECT * FROM cli_graphs WHERE graph_id = ?", (graph_id,)
        ).fetchone()
        return GraphRow(**dict(row)) if row else None

    def graphs_named(self, name: str, project: str | None) -> tuple[GraphRow, ...]:
        if project is None:
            rows = self._connection.execute("SELECT * FROM cli_graphs WHERE name = ?", (name,))
        else:
            rows = self._connection.execute(
                "SELECT * FROM cli_graphs WHERE name = ? AND project = ?", (name, project)
            )
        return tuple(GraphRow(**dict(row)) for row in rows)

    def insert_graph(self, row: GraphRow) -> None:
        self._connection.execute(
            "INSERT INTO cli_graphs VALUES (?, ?, ?, ?, ?, ?)",
            (row.graph_id, row.project, row.name, row.latest_version_id, row.created_at, row.updated_at),
        )

    def set_latest_version(self, graph_id: str, version_id: str, at: str) -> None:
        self._connection.execute(
            "UPDATE cli_graphs SET latest_version_id = ?, updated_at = ? WHERE graph_id = ?",
            (version_id, at, graph_id),
        )

    def versions(self, graph_id: str | None = None) -> tuple[VersionRow, ...]:
        if graph_id is None:
            rows = self._connection.execute("SELECT * FROM cli_graph_versions ORDER BY created_at")
        else:
            rows = self._connection.execute(
                "SELECT * FROM cli_graph_versions WHERE graph_id = ? ORDER BY number", (graph_id,)
            )
        return tuple(_version(row) for row in rows)

    def version(self, version_id: str) -> VersionRow | None:
        row = self._connection.execute(
            "SELECT * FROM cli_graph_versions WHERE version_id = ?", (version_id,)
        ).fetchone()
        return _version(row) if row else None

    def version_numbered(self, graph_id: str, number: int) -> VersionRow | None:
        row = self._connection.execute(
            "SELECT * FROM cli_graph_versions WHERE graph_id = ? AND number = ?", (graph_id, number)
        ).fetchone()
        return _version(row) if row else None

    def insert_version(self, row: VersionRow) -> None:
        self._connection.execute(
            "INSERT INTO cli_graph_versions (version_id, graph_id, number, manifest, digest, "
            "created_at, format, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.version_id, row.graph_id, row.number, json.dumps(row.manifest), row.digest,
                row.created_at, row.format, row.source,
            ),
        )

    # --- run submissions ----------------------------------------------------

    def submission(self, key: str) -> SubmissionRow | None:
        row = self._connection.execute(
            "SELECT * FROM cli_run_submissions WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return SubmissionRow(**dict(row)) if row else None

    def submission_for_run(self, run_id: str) -> SubmissionRow | None:
        row = self._connection.execute(
            "SELECT * FROM cli_run_submissions WHERE run_id = ?", (run_id,)
        ).fetchone()
        return SubmissionRow(**dict(row)) if row else None

    def insert_submission(self, row: SubmissionRow) -> None:
        self._connection.execute(
            "INSERT INTO cli_run_submissions VALUES (?, ?, ?, ?, ?)",
            (row.idempotency_key, row.request_digest, row.run_id, row.version_id, row.created_at),
        )

    def started_runs(
        self,
        *,
        project: str | None = None,
        graph_id: str | None = None,
        loop_id: str | None = None,
        requester: str | None = None,
        origin: str | None = None,
    ) -> tuple[StartedRunRow, ...]:
        """Every graph run, with optional CLI registration and loop information."""
        clauses, parameters = [], []
        # Unregistered workorders have no CLI project and must remain visible
        # when the CLI supplies its default project filter.
        for column, value in (("g.project", project), ("g.graph_id", graph_id), ("lr.loop_id", loop_id), ("r.requester", requester), ("r.origin_channel", origin)):
            if value is not None:
                clauses.append(f"({column} = ? OR g.project IS NULL)" if column == "g.project" else f"{column} = ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT DISTINCT r.run_id, COALESCE(s.version_id, l.version_id) AS version_id,
                r.started_at, lr.loop_id
            FROM runs r
            LEFT JOIN cli_run_submissions s ON s.run_id = r.run_id
            LEFT JOIN cli_loop_runs lr ON lr.run_id = r.run_id
            LEFT JOIN cli_loops l ON l.loop_id = lr.loop_id
            LEFT JOIN cli_graph_versions v ON v.version_id = COALESCE(s.version_id, l.version_id)
            LEFT JOIN cli_graphs g ON g.graph_id = v.graph_id
            {where}
            ORDER BY r.created_at DESC, r.run_id DESC
            """,
            parameters,
        )
        return tuple(StartedRunRow(**dict(row)) for row in rows)

    # --- loops --------------------------------------------------------------

    def loops(self, project: str | None = None) -> tuple[LoopRow, ...]:
        if project is None:
            rows = self._connection.execute("SELECT * FROM cli_loops ORDER BY project, name")
        else:
            rows = self._connection.execute(
                "SELECT * FROM cli_loops WHERE project = ? ORDER BY name", (project,)
            )
        return tuple(_loop(row) for row in rows)

    def loop(self, loop_id: str) -> LoopRow | None:
        row = self._connection.execute("SELECT * FROM cli_loops WHERE loop_id = ?", (loop_id,)).fetchone()
        return _loop(row) if row else None

    def loops_named(self, name: str, project: str | None) -> tuple[LoopRow, ...]:
        if project is None:
            rows = self._connection.execute("SELECT * FROM cli_loops WHERE name = ?", (name,))
        else:
            rows = self._connection.execute(
                "SELECT * FROM cli_loops WHERE name = ? AND project = ?", (name, project)
            )
        return tuple(_loop(row) for row in rows)

    def insert_loop(self, row: LoopRow) -> None:
        self._connection.execute(
            "INSERT INTO cli_loops VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.loop_id, row.project, row.name, row.graph_id, row.version_id,
                row.instruction, row.repository, json.dumps(row.inputs),
                row.interval_seconds, row.max_prs, row.max_spend_usd, row.state,
                row.pause_reason, row.next_run_at, row.active_run_id,
                row.created_at, row.updated_at, row.last_error, row.last_error_at,
                row.consecutive_start_failures,
            ),
        )

    def update_loop(self, loop_id: str, **changes: object) -> None:
        allowed = {
            "state", "pause_reason", "next_run_at", "active_run_id", "updated_at",
            "max_prs", "max_spend_usd", "last_error", "last_error_at", "consecutive_start_failures",
        }
        assert set(changes) <= allowed, changes
        columns = ", ".join(f"{name} = ?" for name in changes)
        self._connection.execute(
            f"UPDATE cli_loops SET {columns} WHERE loop_id = ?", (*changes.values(), loop_id)
        )

    def loop_runs(self, loop_id: str) -> tuple[tuple[str, str], ...]:
        """`(tick, run_id)` in the order they were started."""
        rows = self._connection.execute(
            "SELECT tick, run_id FROM cli_loop_runs WHERE loop_id = ? ORDER BY started_at, tick",
            (loop_id,),
        )
        return tuple((row["tick"], row["run_id"]) for row in rows)

    def loop_for_run(self, run_id: str) -> str | None:
        row = self._connection.execute(
            "SELECT loop_id FROM cli_loop_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return row["loop_id"] if row else None

    def claim_tick(self, connection: sqlite3.Connection, loop_id: str, tick: str, at: str) -> bool:
        """Reserve one scheduled tick; false when something already started it."""
        try:
            connection.execute(
                "INSERT INTO cli_loop_runs VALUES (?, ?, '', ?)", (loop_id, tick, at)
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def set_tick_run(self, loop_id: str, tick: str, run_id: str) -> None:
        self._connection.execute(
            "UPDATE cli_loop_runs SET run_id = ? WHERE loop_id = ? AND tick = ?", (run_id, loop_id, tick)
        )

    def release_tick(self, loop_id: str, tick: str) -> None:
        self._connection.execute(
            "DELETE FROM cli_loop_runs WHERE loop_id = ? AND tick = ? AND run_id = ''", (loop_id, tick)
        )

    # --- node executions ----------------------------------------------------

    def executions(self, run_id: str) -> tuple[ExecutionRow, ...]:
        rows = self._connection.execute(
            "SELECT * FROM cli_node_executions WHERE run_id = ? ORDER BY started_at, rowid", (run_id,)
        )
        return tuple(ExecutionRow(**dict(row)) for row in rows)

    def execution(self, execution_id: str) -> ExecutionRow | None:
        row = self._connection.execute(
            "SELECT * FROM cli_node_executions WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return ExecutionRow(**dict(row)) if row else None

    def start_execution(self, run_id: str, node_id: str, execution_id: str, at: str) -> None:
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM cli_node_executions WHERE execution_id = ?", (execution_id,)
            ).fetchone():
                return
            (attempts,) = connection.execute(
                "SELECT COUNT(*) FROM cli_node_executions WHERE run_id = ? AND node_id = ?",
                (run_id, node_id),
            ).fetchone()
            connection.execute(
                "INSERT INTO cli_node_executions VALUES (?, ?, ?, ?, 'running', ?, NULL, '')",
                (execution_id, run_id, node_id, attempts + 1, at),
            )

    def set_execution_status(
        self, execution_id: str, status: str, at: str | None = None, error: str = ""
    ) -> None:
        """Move an open execution; a finished one keeps the status it ended with."""
        finished = at if status in TERMINAL_EXECUTION_STATUSES else None
        self._connection.execute(
            "UPDATE cli_node_executions SET status = ?, finished_at = COALESCE(?, finished_at), "
            "error = CASE WHEN ? != '' THEN ? ELSE error END "
            "WHERE execution_id = ? AND status IN ('running', 'awaiting_approval')",
            (status, finished, error, error, execution_id),
        )

    def close_open_executions(
        self, status: str, at: str, *, run_id: str | None = None, node_id: str | None = None, error: str = ""
    ) -> None:
        query = (
            "UPDATE cli_node_executions SET status = ?, finished_at = ?, "
            "error = CASE WHEN ? != '' THEN ? ELSE error END "
            "WHERE status IN ('running', 'awaiting_approval')"
        )
        parameters: list[object] = [status, at, error, error]
        if run_id is not None:
            query += " AND run_id = ?"
            parameters.append(run_id)
        if node_id is not None:
            query += " AND node_id = ?"
            parameters.append(node_id)
        self._connection.execute(query, parameters)

    # --- steering -----------------------------------------------------------

    def steering(self, execution_id: str) -> tuple[SteeringRow, ...]:
        rows = self._connection.execute(
            "SELECT * FROM cli_steering WHERE execution_id = ? ORDER BY sequence", (execution_id,)
        )
        return tuple(SteeringRow(**dict(row)) for row in rows)

    def steering_by_key(self, key: str) -> SteeringRow | None:
        row = self._connection.execute(
            "SELECT * FROM cli_steering WHERE idempotency_key = ?", (key,)
        ).fetchone()
        return SteeringRow(**dict(row)) if row else None

    def steering_by_id(self, steering_id: str) -> SteeringRow | None:
        row = self._connection.execute(
            "SELECT * FROM cli_steering WHERE steering_id = ?", (steering_id,)
        ).fetchone()
        return SteeringRow(**dict(row)) if row else None

    def insert_steering(
        self, steering_id: str, key: str, run_id: str, execution_id: str, message: str, at: str
    ) -> None:
        self._connection.execute(
            "INSERT INTO cli_steering (steering_id, idempotency_key, run_id, execution_id, "
            "message, status, accepted_at) VALUES (?, ?, ?, ?, ?, 'accepted', ?)",
            (steering_id, key, run_id, execution_id, message, at),
        )

    def mark_steering(self, steering_id: str, status: str, at: str, error: str = "") -> None:
        """Advance a message; it never moves backwards or past a final state."""
        column = {"delivered": "delivered_at", "applied": "applied_at"}.get(status)
        allowed_from = {
            "delivered": ("accepted",),
            "applied": ("accepted", "delivered"),
            "rejected": ("accepted",),
            "undelivered": ("accepted",),
        }[status]
        placeholders = ", ".join("?" for _ in allowed_from)
        assignments = "status = ?, error = ?"
        parameters: list[object] = [status, error]
        if column:
            assignments += f", {column} = ?"
            parameters.append(at)
            if status == "applied":
                assignments += ", delivered_at = COALESCE(delivered_at, ?)"
                parameters.append(at)
        self._connection.execute(
            f"UPDATE cli_steering SET {assignments} WHERE steering_id = ? AND status IN ({placeholders})",
            (*parameters, steering_id, *allowed_from),
        )

    def abandon_undelivered_steering(
        self, error: str, *, execution_id: str | None = None, run_id: str | None = None
    ) -> None:
        """Messages a live queue held when its execution ended or the process stopped."""
        query = "UPDATE cli_steering SET status = 'undelivered', error = ? WHERE status = 'accepted'"
        parameters: list[object] = [error]
        if execution_id is not None:
            query += " AND execution_id = ?"
            parameters.append(execution_id)
        if run_id is not None:
            query += " AND run_id = ?"
            parameters.append(run_id)
        self._connection.execute(query, parameters)


def _version(row: sqlite3.Row) -> VersionRow:
    values = dict(row)
    values["manifest"] = json.loads(values["manifest"])
    return VersionRow(**values)


def _loop(row: sqlite3.Row) -> LoopRow:
    values: Mapping[str, Any] = dict(row)
    return LoopRow(**{**values, "inputs": json.loads(values["inputs"])})


__all__ = [
    "AgentRow",
    "ExecutionRow",
    "GraphRow",
    "GraphServiceStore",
    "LoopRow",
    "OPEN_EXECUTION_STATUSES",
    "SteeringRow",
    "SubmissionRow",
    "TERMINAL_EXECUTION_STATUSES",
    "VersionRow",
    "now_iso",
    "parse_iso",
]

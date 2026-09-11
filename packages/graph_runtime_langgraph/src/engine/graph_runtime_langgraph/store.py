"""What has to outlive the process, and nothing that does not.

LangGraph's checkpointer already persists the only difficult thing: where a run
is and what its state holds. Other facts sit beside it that a checkpoint has no
place for, and they are needed by a process that did not start the run:

* which graph a run is of, so a fresh runtime can load the right compiled graph
  for a thread id it has never seen;
* what an execution asked a person, so a request raised before a restart is
  still answerable afterwards;
* how to reach the ACP conversation that asked, so answering it continues the
  same agent session rather than starting a second one;
* the event history, so transcripts and tool results survive a restart and can
  be supplied when steering reopens a finished conversation.

That last one is the reason this module exists at all. An agent that wants
permission to run a command is not something to keep a coroutine alive for --
the person may answer in a minute or on Monday -- so the request is written down
and the process is free to stop. See `engine.graph_runtime_langgraph.acp` for
what is done with the record on the way back in.

The continuation stored is `langgraph_acp.ACPContinuation` verbatim, serialized
with its own `to_dict`. Deliberately: reconnecting is `langgraph-acp`'s
mechanism, and a store that invented its own fields for the same facts would be
a second definition of an ACP session's identity that could disagree with the
first.

`SqliteGraphRuntimeStore` is the one to deploy. `InMemoryGraphRuntimeStore` is
for tests whose runs never leave the process, and is explicit about it in the
same way `langgraph_acp.InMemoryACPSessionStore` is: a durability test that used
it would be testing a dictionary.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from engine.domain import (
    ApprovalDecision,
    ApprovalId,
    ApprovalKind,
    ApprovalStatus,
    RunId,
)
from langgraph_acp import ACPContinuation
from migrations.migration import upgrade_connection

from engine.graph_runtime.events import EventKind, EventStore, RuntimeEvent
from engine.graph_runtime.identity import ExecutionId
from engine.graph_runtime.topology import GraphId, NodeId


@dataclass(frozen=True, slots=True)
class RunRecord:
    """The little about a run that is not in its LangGraph thread."""

    run_id: RunId
    graph_id: GraphId
    error: str = ""
    """Why it stopped, when it stopped badly. Cleared by a fork."""
    auto_approve_nodes: tuple[NodeId, ...] = ()
    runner_overrides: Mapping[NodeId, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One question an execution asked, and the answer if it has one.

    Everything needed to render the request, to route the answer, and -- through
    `continuation` -- to reach the conversation that is waiting for it. The
    request's own ACP payload is kept in `request` so that a process which never
    saw the agent ask can still tell it which of the options it offered was
    chosen.
    """

    approval_id: ApprovalId
    run_id: RunId
    execution_id: ExecutionId
    node_id: NodeId
    kind: ApprovalKind = ApprovalKind.COMMAND_EXECUTION
    reason: str = ""
    command: str = ""
    tool_name: str = ""
    allowed_decisions: tuple[ApprovalDecision, ...] = (
        ApprovalDecision.ACCEPT,
        ApprovalDecision.CANCEL,
    )
    session_key: str = ""
    """Which ACP conversation within the run raised it, if one did."""
    continuation: ACPContinuation | None = None
    """How to reach that conversation again. `None` for a non-ACP execution."""
    request: Mapping[str, object] = field(default_factory=dict)
    """The agent's own `session/request_permission` payload."""
    cancel_run: bool = True
    """Whether refusing this request also refuses the graph execution."""
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: ApprovalDecision | None = None
    """The answer, once somebody gave one. `None` for the other two states.

    Which is why `status` is here rather than being read off this field. An
    `INTERRUPTED` request has no decision and never will -- the execution that
    asked is gone -- but it is also not pending, and a node re-entered later
    must be able to tell the two apart: one has an answer to apply, and the
    other is a question that died with the attempt that asked it.
    """

    @property
    def pending(self) -> bool:
        return self.status is ApprovalStatus.PENDING


@dataclass(frozen=True, slots=True)
class CommentRecord:
    """One comment a run left on a GitHub pull request.

    Kept outside the event history because it is about the forge rather than
    about this run: what a later run needs is "which comments are already on
    this pull request", and answering that from a run's own transcript would
    mean replaying every run that ever touched it.

    GitHub only, as the table name says. Another forge numbers its notes from
    its own counter, so filing them here would let two unrelated comments claim
    one row; a forge that needs remembering gets a table of its own.
    """

    comment_id: int
    """GitHub's id for the comment, unique only within `repository` and `kind`."""
    repository: str
    """Canonical lowercase owner/repo; prefixed with the host outside github.com."""
    kind: str
    """`issue` or `review`: which of GitHub's two id spaces `comment_id` is in.

    GitHub numbers conversation comments and inline review comments from
    separate sequences, so the two can hand out the same id for different
    comments; together with `repository` this is what keeps them apart.
    """
    pr_number: int
    run_id: RunId
    posted_at: str
    """When it was posted, ISO 8601, as the caller that posted it saw the clock."""
    node_id: NodeId | None = None
    """Which node posted it. `None` outside a graph."""
    url: str = ""


@dataclass(frozen=True, slots=True)
class PullRequestRecord:
    """The run working on one GitHub pull request.

    Ownership of a pull request is created by taking it on -- usually by
    opening it, or by being started to work on one that nobody is -- so it is
    written down when that happens rather than inferred afterwards. Inferring
    it from the comments on the pull request cannot work: every run that
    comments is recorded there, so a review, a status update, or any later
    follow-up would displace the run that actually did the work.

    One row per pull request, replaced when another run takes it on -- a
    re-opened pull request belongs to whoever opened it last, and one whose
    work order has finished belongs to whoever was started to carry it on,
    which is still an act of taking it on rather than of commenting.
    """

    repository: str
    """Canonical lowercase owner/repo; prefixed with the host outside github.com."""
    number: int
    run_id: RunId
    opened_at: str
    """When it was taken on, ISO 8601, as the caller that did so saw the clock."""
    node_id: NodeId | None = None
    """Which node took it on. `None` outside a graph."""
    url: str = ""


@runtime_checkable
class GraphRuntimeStore(EventStore, Protocol):
    """The durable half of the runtime, including the event history."""

    async def remember_run(self, record: RunRecord) -> None:
        """Record a run, replacing what was known about it."""
        ...

    async def run(self, run_id: RunId) -> RunRecord | None: ...

    async def runs(self) -> tuple[RunRecord, ...]:
        """Every run this store knows of, oldest first."""
        ...

    async def remember_session(
        self, run_id: RunId, session_key: str, continuation: ACPContinuation
    ) -> None:
        """Bind a logical agent within a run to the ACP conversation it holds."""
        ...

    async def session(
        self, run_id: RunId, session_key: str
    ) -> ACPContinuation | None: ...

    async def forget_session(self, run_id: RunId, session_key: str) -> None: ...

    async def remember_approval(self, record: ApprovalRecord) -> None: ...

    async def approval(self, approval_id: ApprovalId) -> ApprovalRecord | None: ...

    async def pending_approvals(self, run_id: RunId) -> tuple[ApprovalRecord, ...]:
        """Unanswered requests for this run, in the order they were raised."""
        ...

    async def resolve_approval(
        self, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> None:
        """Write the answer down before anyone acts on it."""
        ...

    async def remember_comment(self, record: CommentRecord) -> None:
        """Record a comment a run posted, replacing what was known about it."""
        ...

    async def comments(self, run_id: RunId) -> tuple[CommentRecord, ...]:
        """Every comment this run posted, oldest first."""
        ...

    async def remember_pull_request(self, record: PullRequestRecord) -> None:
        """Record which run opened a pull request, replacing any earlier claim."""
        ...

    async def claim_pull_request(
        self, record: PullRequestRecord, *, replacing: RunId | None = None
    ) -> RunId:
        """Take a pull request on if it is free, and say who ended up with it.

        The conditional twin of `remember_pull_request`, for a caller that
        needs one run per pull request rather than the newest one: free means
        unclaimed, or still claimed by `replacing` -- the run the caller
        already established has stopped working, which is what makes taking it
        over legitimate. Everyone else is told the current holder's id instead
        of displacing it, so two callers racing to take the same pull request
        on agree on the winner, and the loser can undo what it started.
        Replacing unconditionally would leave that run alive and unreachable,
        since every later comment is routed by this row.
        """
        ...

    async def run_for_pull_request(self, repository: str, number: int) -> RunId | None:
        """Which run opened this pull request.

        A caller holding a pull request -- a webhook answering a comment on it
        -- needs the run without knowing a run id, and the alternative is
        reading every run's state to find the one that matches.

        Answered from what was written when the pull request was opened, not
        from the comments on it. Commenting is something any run may do to a
        pull request it does not own, so the newest commenter is not the owner:
        a review or a follow-up run would otherwise inherit the feedback meant
        for the work order that opened it.

        ``None`` when no run opened it, which is the honest answer for a pull
        request opened by hand or before this was recorded.
        """
        ...

    async def abandon_run_approvals(self, run_id: RunId) -> None:
        """Settle every open request this run raised, without deciding one.

        What a fork and a refusal both do to the questions still outstanding: an
        execution that has been stopped cannot be told anything, so its question
        can never be answered, and leaving it pending would report a run that is
        over as still waiting on a person.

        Settled rather than deleted, because a client may still be showing one.
        It should be told the request is no longer pending, not that it never
        existed.
        """
        ...


class InMemoryGraphRuntimeStore:
    """A store that lasts exactly as long as the process does.

    Right for a run that begins and ends inside one process, and wrong the
    moment durability is the point: a run recovered from a checkpoint written
    before a restart would find no graph to load it into and no record of the
    approval it stopped at.
    """

    def __init__(self) -> None:
        self._events: dict[RunId, list[RuntimeEvent]] = {}
        self._runs: dict[RunId, RunRecord] = {}
        self._sessions: dict[tuple[RunId, str], ACPContinuation] = {}
        self._approvals: dict[ApprovalId, ApprovalRecord] = {}
        self._comments: dict[tuple[str, str, int], CommentRecord] = {}
        self._pull_requests: dict[tuple[str, int], PullRequestRecord] = {}

    def append_event(self, event: RuntimeEvent) -> RuntimeEvent:
        events = self._events.setdefault(event.run_id, [])
        numbered = replace(event, sequence=len(events) + 1)
        events.append(numbered)
        return numbered

    def events_since(self, run_id: RunId, cursor: int = 0) -> tuple[RuntimeEvent, ...]:
        return tuple(self._events.get(run_id, ())[cursor:])

    async def remember_run(self, record: RunRecord) -> None:
        self._runs[record.run_id] = record

    async def run(self, run_id: RunId) -> RunRecord | None:
        return self._runs.get(run_id)

    async def runs(self) -> tuple[RunRecord, ...]:
        return tuple(self._runs.values())

    async def remember_session(
        self, run_id: RunId, session_key: str, continuation: ACPContinuation
    ) -> None:
        self._sessions[(run_id, session_key)] = continuation

    async def session(self, run_id: RunId, session_key: str) -> ACPContinuation | None:
        return self._sessions.get((run_id, session_key))

    async def forget_session(self, run_id: RunId, session_key: str) -> None:
        self._sessions.pop((run_id, session_key), None)

    async def remember_approval(self, record: ApprovalRecord) -> None:
        self._approvals[record.approval_id] = record

    async def approval(self, approval_id: ApprovalId) -> ApprovalRecord | None:
        return self._approvals.get(approval_id)

    async def pending_approvals(self, run_id: RunId) -> tuple[ApprovalRecord, ...]:
        return tuple(
            record
            for record in self._approvals.values()
            if record.run_id == run_id and record.pending
        )

    async def resolve_approval(
        self, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> None:
        record = self._approvals.get(approval_id)
        if record is not None:
            self._approvals[approval_id] = replace(
                record, status=ApprovalStatus.DECIDED, decision=decision
            )

    async def remember_comment(self, record: CommentRecord) -> None:
        self._comments[(record.repository, record.kind, record.comment_id)] = record

    async def comments(self, run_id: RunId) -> tuple[CommentRecord, ...]:
        return tuple(
            record for record in self._comments.values() if record.run_id == run_id
        )

    async def remember_pull_request(self, record: PullRequestRecord) -> None:
        self._pull_requests[(record.repository, record.number)] = record

    async def claim_pull_request(
        self, record: PullRequestRecord, *, replacing: RunId | None = None
    ) -> RunId:
        held = self._pull_requests.get((record.repository, record.number))
        if held is not None and held.run_id != replacing:
            return held.run_id
        self._pull_requests[(record.repository, record.number)] = record
        return record.run_id

    async def run_for_pull_request(self, repository: str, number: int) -> RunId | None:
        opened = self._pull_requests.get((repository, number))
        return None if opened is None else opened.run_id

    async def abandon_run_approvals(self, run_id: RunId) -> None:
        for approval_id, record in tuple(self._approvals.items()):
            if record.run_id == run_id and record.pending:
                self._approvals[approval_id] = replace(
                    record, status=ApprovalStatus.INTERRUPTED
                )


class SqliteGraphRuntimeStore:
    """The same store, in a file that outlives the interpreter.

    Plain `sqlite3` rather than a driver: every method is a single statement
    against a local file, so the connection never blocks long enough to be worth
    a thread, and the alternative is a dependency for three tables.

    Every method is `async` regardless, because the point of the protocol is
    that swapping this for a Postgres store is a constructor argument rather
    than an edit to every caller.
    """

    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        upgrade_connection(self._connection, store="graph")
        self._ordinal = 0

    def append_event(self, event: RuntimeEvent) -> RuntimeEvent:
        cursor = self._connection.execute(
            "INSERT INTO events (run_id, kind, payload, node_id, execution_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(event.run_id), event.kind.value, json.dumps(dict(event.payload)),
             event.node_id, event.execution_id),
        )
        assert cursor.lastrowid is not None
        return replace(event, sequence=cursor.lastrowid)

    def events_since(self, run_id: RunId, cursor: int = 0) -> tuple[RuntimeEvent, ...]:
        rows = self._connection.execute(
            "SELECT * FROM events WHERE run_id = ? AND sequence > ? ORDER BY sequence",
            (str(run_id), cursor),
        ).fetchall()
        return tuple(RuntimeEvent(
            run_id=RunId(row["run_id"]), kind=EventKind(row["kind"]),
            payload=json.loads(row["payload"]), sequence=row["sequence"],
            node_id=NodeId(row["node_id"]) if row["node_id"] else None,
            execution_id=ExecutionId(row["execution_id"]) if row["execution_id"] else None,
        ) for row in rows)

    def close(self) -> None:
        self._connection.close()

    def _next(self) -> int:
        self._ordinal += 1
        return self._ordinal

    async def remember_run(self, record: RunRecord) -> None:
        self._connection.execute(
            "INSERT INTO runs (run_id, graph_id, error, ordinal, auto_approve_nodes, runner_overrides) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (run_id) DO UPDATE SET graph_id = excluded.graph_id, "
            "error = excluded.error, auto_approve_nodes = excluded.auto_approve_nodes, "
            "runner_overrides = excluded.runner_overrides",
            (
                str(record.run_id),
                str(record.graph_id),
                record.error,
                self._next(),
                json.dumps(record.auto_approve_nodes),
                json.dumps(dict(record.runner_overrides)),
            ),
        )

    async def run(self, run_id: RunId) -> RunRecord | None:
        row = self._connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (str(run_id),)
        ).fetchone()
        return None if row is None else _run_from(row)

    async def runs(self) -> tuple[RunRecord, ...]:
        rows = self._connection.execute("SELECT * FROM runs ORDER BY ordinal").fetchall()
        return tuple(_run_from(row) for row in rows)

    async def remember_session(
        self, run_id: RunId, session_key: str, continuation: ACPContinuation
    ) -> None:
        self._connection.execute(
            "INSERT INTO sessions (run_id, session_key, continuation) VALUES (?, ?, ?) "
            "ON CONFLICT (run_id, session_key) DO UPDATE SET "
            "continuation = excluded.continuation",
            (str(run_id), session_key, json.dumps(continuation.to_dict())),
        )

    async def session(self, run_id: RunId, session_key: str) -> ACPContinuation | None:
        row = self._connection.execute(
            "SELECT continuation FROM sessions WHERE run_id = ? AND session_key = ?",
            (str(run_id), session_key),
        ).fetchone()
        if row is None:
            return None
        return ACPContinuation.from_dict(json.loads(row["continuation"]))

    async def forget_session(self, run_id: RunId, session_key: str) -> None:
        self._connection.execute(
            "DELETE FROM sessions WHERE run_id = ? AND session_key = ?",
            (str(run_id), session_key),
        )

    async def remember_approval(self, record: ApprovalRecord) -> None:
        self._connection.execute(
            "INSERT INTO approvals "
            "(approval_id, run_id, record, status, decision, ordinal) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (approval_id) DO UPDATE SET "
            "record = excluded.record, status = excluded.status, "
            "decision = excluded.decision",
            (
                str(record.approval_id),
                str(record.run_id),
                json.dumps(_approval_to_json(record)),
                record.status.value,
                None if record.decision is None else record.decision.value,
                self._next(),
            ),
        )

    async def approval(self, approval_id: ApprovalId) -> ApprovalRecord | None:
        row = self._connection.execute(
            "SELECT * FROM approvals WHERE approval_id = ?", (str(approval_id),)
        ).fetchone()
        return None if row is None else _approval_from(row)

    async def pending_approvals(self, run_id: RunId) -> tuple[ApprovalRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM approvals WHERE run_id = ? AND status = ? ORDER BY ordinal",
            (str(run_id), ApprovalStatus.PENDING.value),
        ).fetchall()
        return tuple(_approval_from(row) for row in rows)

    async def resolve_approval(
        self, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> None:
        self._connection.execute(
            "UPDATE approvals SET status = ?, decision = ? WHERE approval_id = ?",
            (ApprovalStatus.DECIDED.value, decision.value, str(approval_id)),
        )

    async def remember_comment(self, record: CommentRecord) -> None:
        self._connection.execute(
            "INSERT INTO github_comments "
            "(comment_id, repository, kind, pr_number, run_id, node_id, posted_at, url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (repository, kind, comment_id) DO UPDATE SET "
            "pr_number = excluded.pr_number, run_id = excluded.run_id, "
            "node_id = excluded.node_id, posted_at = excluded.posted_at, "
            "url = excluded.url",
            (
                record.comment_id,
                record.repository,
                record.kind,
                record.pr_number,
                str(record.run_id),
                None if record.node_id is None else str(record.node_id),
                record.posted_at,
                record.url,
            ),
        )

    async def comments(self, run_id: RunId) -> tuple[CommentRecord, ...]:
        rows = self._connection.execute(
            "SELECT * FROM github_comments WHERE run_id = ? ORDER BY posted_at, comment_id",
            (str(run_id),),
        ).fetchall()
        return tuple(_comment_from(row) for row in rows)

    async def remember_pull_request(self, record: PullRequestRecord) -> None:
        self._connection.execute(
            "INSERT INTO github_pull_requests "
            "(repository, number, run_id, node_id, opened_at, url) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (repository, number) DO UPDATE SET "
            "run_id = excluded.run_id, node_id = excluded.node_id, "
            "opened_at = excluded.opened_at, url = excluded.url",
            (
                record.repository,
                record.number,
                str(record.run_id),
                None if record.node_id is None else str(record.node_id),
                record.opened_at,
                record.url,
            ),
        )

    async def claim_pull_request(
        self, record: PullRequestRecord, *, replacing: RunId | None = None
    ) -> RunId:
        # The write is the claim, and the database decides it: the insert takes
        # a free pull request, and the conditional update takes one still held
        # by the run the caller saw stop. Anyone racing this loses at the same
        # point, whatever else happened in between, and the read afterwards is
        # of committed state, so the loser is told who won rather than left
        # thinking it did. `replacing` of `None` matches no row, because a
        # claimed pull request always names a run.
        self._connection.execute(
            "INSERT INTO github_pull_requests "
            "(repository, number, run_id, node_id, opened_at, url) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (repository, number) DO UPDATE SET "
            "run_id = excluded.run_id, node_id = excluded.node_id, "
            "opened_at = excluded.opened_at, url = excluded.url "
            "WHERE github_pull_requests.run_id = ?",
            (
                record.repository,
                record.number,
                str(record.run_id),
                None if record.node_id is None else str(record.node_id),
                record.opened_at,
                record.url,
                None if replacing is None else str(replacing),
            ),
        )
        held = await self.run_for_pull_request(record.repository, record.number)
        assert held is not None  # just inserted, if it was not already there
        return held

    async def run_for_pull_request(self, repository: str, number: int) -> RunId | None:
        # One row per pull request, found by its primary key, so this stays a
        # single seek however many runs the deployment has accumulated.
        row = self._connection.execute(
            "SELECT run_id FROM github_pull_requests "
            "WHERE repository = ? AND number = ?",
            (repository, number),
        ).fetchone()
        return None if row is None else RunId(row["run_id"])

    async def abandon_run_approvals(self, run_id: RunId) -> None:
        self._connection.execute(
            "UPDATE approvals SET status = ? WHERE run_id = ? AND status = ?",
            (
                ApprovalStatus.INTERRUPTED.value,
                str(run_id),
                ApprovalStatus.PENDING.value,
            ),
        )


def _run_from(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=RunId(row["run_id"]),
        graph_id=GraphId(row["graph_id"]),
        error=row["error"],
        runner_overrides=json.loads(row["runner_overrides"]),
        auto_approve_nodes=tuple(
            NodeId(node) for node in json.loads(row["auto_approve_nodes"])
        ),
    )


def _comment_from(row: sqlite3.Row) -> CommentRecord:
    return CommentRecord(
        comment_id=row["comment_id"],
        repository=row["repository"],
        kind=row["kind"],
        pr_number=row["pr_number"],
        run_id=RunId(row["run_id"]),
        posted_at=row["posted_at"],
        node_id=NodeId(row["node_id"]) if row["node_id"] else None,
        url=row["url"] or "",
    )


def _approval_to_json(record: ApprovalRecord) -> dict[str, object]:
    return {
        "approval_id": str(record.approval_id),
        "run_id": str(record.run_id),
        "execution_id": str(record.execution_id),
        "node_id": str(record.node_id),
        "kind": record.kind.value,
        "reason": record.reason,
        "command": record.command,
        "tool_name": record.tool_name,
        "allowed_decisions": [
            decision.value for decision in record.allowed_decisions
        ],
        "session_key": record.session_key,
        "continuation": (
            None if record.continuation is None else record.continuation.to_dict()
        ),
        "request": dict(record.request),
        "cancel_run": record.cancel_run,
    }


def _approval_from(row: sqlite3.Row) -> ApprovalRecord:
    stored = json.loads(row["record"])
    continuation = stored.get("continuation")
    allowed: Sequence[str] = stored.get("allowed_decisions") or []
    return ApprovalRecord(
        approval_id=ApprovalId(stored["approval_id"]),
        run_id=RunId(stored["run_id"]),
        execution_id=ExecutionId(stored["execution_id"]),
        node_id=NodeId(stored["node_id"]),
        kind=ApprovalKind(stored["kind"]),
        reason=stored["reason"],
        command=stored["command"],
        tool_name=stored["tool_name"],
        allowed_decisions=tuple(ApprovalDecision(value) for value in allowed),
        session_key=stored["session_key"],
        continuation=(
            None if continuation is None else ACPContinuation.from_dict(continuation)
        ),
        request=stored.get("request") or {},
        cancel_run=stored.get("cancel_run", True),
        status=ApprovalStatus(row["status"]),
        decision=(
            None if row["decision"] is None else ApprovalDecision(row["decision"])
        ),
    )


__all__ = [
    "ApprovalRecord",
    "CommentRecord",
    "GraphRuntimeStore",
    "InMemoryGraphRuntimeStore",
    "RunRecord",
    "SqliteGraphRuntimeStore",
]

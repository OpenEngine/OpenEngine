"""The contract the HTTP surface is written against.

`GraphRuntime` is everything the control server needs a graph to be able to do,
and nothing about how a graph does it. LangGraph is the intended implementation
and the tests drive a scripted one, which is the point of stating it as a
protocol: the server, its wire format, and its behaviour under steering and
manual transition are all buildable and checkable before the binding exists, and
the same tests then run against the binding unchanged.

Failures are exceptions rather than sentinel snapshots. A control surface has to
be able to tell "there is no such run" from "that run cannot be steered right
now", because the first is the client's mistake and the second is a race it
should retry -- and a `None` cannot say which it was.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from engine.domain import ApprovalDecision, ApprovalId, ApprovalKind, RunId

from engine.langgraph_runtime.events import EventObserver
from engine.langgraph_runtime.topology import GraphId, GraphTopology, NodeId


class RunStatus(Enum):
    """Where a run is, as far as anyone outside it can tell."""

    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    """Paused on a question. Nothing else will happen until it is answered."""
    COMPLETED = "completed"
    FAILED = "failed"
    """Stopped without finishing, with `RunSnapshot.error` saying why.

    A node raising is the ordinary way here, not an exceptional one, so it is
    part of the contract rather than something a binding may leave undefined: an
    implementation must publish `run.failed` and reach this status, in the node
    that raised. A run whose task died silently would report `running` forever,
    hold a subscriber waiting for a terminal event that never came, and refuse
    steering as having no node in flight -- three answers a client cannot
    reconcile, and no way to tell it apart from a node that is still thinking.
    """


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One thing a node has stopped to ask for.

    Kept in fields rather than a rendered sentence, for the reason
    `engine.domain.approvals` keeps its record that way: a stored `command` can
    be compared and shown differently by a different client.
    """

    approval_id: ApprovalId
    node_id: NodeId
    kind: ApprovalKind
    reason: str = ""
    command: str = ""
    tool_name: str = ""
    allowed_decisions: tuple[ApprovalDecision, ...] = (
        ApprovalDecision.ACCEPT,
        ApprovalDecision.CANCEL,
    )


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    """One complete answer to "what is this run doing?".

    Whole rather than incremental, like the event log's replay beside it: a
    client that has just arrived cannot reconstruct a run from the parts of it
    that were published before it got there.
    """

    run_id: RunId
    graph_id: GraphId
    status: RunStatus
    current_node: NodeId | None = None
    visited: tuple[NodeId, ...] = ()
    """The nodes this run has entered, in order, most recent last.

    Truncated by a manual transition: a run sent back to `implementation` has
    not visited `review` any more, and a history that kept saying it had would
    make the second pass unreadable.
    """
    values: Mapping[str, object] = field(default_factory=dict)
    """The graph's state, as the graph itself would report it."""
    pending_approvals: tuple[PendingApproval, ...] = ()
    error: str = ""


class LangGraphRuntimeError(Exception):
    """Anything the control surface refuses, in one catchable family."""


class UnknownGraphError(LangGraphRuntimeError):
    """No graph by that id is registered."""


class UnknownRunError(LangGraphRuntimeError):
    """No run by that id, in this process or its store."""


class UnknownNodeError(LangGraphRuntimeError):
    """A transition named a node the run's graph does not have."""


class UnknownApprovalError(LangGraphRuntimeError):
    """No request by that id was raised by this run."""


class ApprovalNotPendingError(LangGraphRuntimeError):
    """The request outlived whatever was waiting on it, or was already answered."""


class RunNotSteerableError(LangGraphRuntimeError):
    """There is no node in flight to deliver the message to."""


@runtime_checkable
class GraphRuntime(Protocol):
    """A graph, driven from outside it."""

    def observe(self, observer: EventObserver) -> None:
        """Install the sink every later event is published to.

        One observer, replacing any earlier one: fan-out is the event log's job,
        and a graph that had to keep a subscriber list would be reimplementing
        it once per binding.
        """
        ...

    def graphs(self) -> tuple[GraphTopology, ...]:
        """Every graph that can be started."""
        ...

    def topology(self, graph_id: GraphId) -> GraphTopology | None:
        """One graph's shape, or `None` when it is not registered."""
        ...

    async def start(
        self, graph_id: GraphId, values: Mapping[str, object]
    ) -> RunSnapshot:
        """Begin a run of `graph_id` with `values` as its initial state.

        Raises `UnknownGraphError` for a graph that is not registered.
        """
        ...

    async def snapshot(self, run_id: RunId) -> RunSnapshot | None:
        """What this run is doing now, or `None` when there is no such run."""
        ...

    async def steer(self, run_id: RunId, message: str) -> RunSnapshot:
        """Deliver `message` to the node currently running.

        The node continues from where it was interrupted rather than starting
        over: steering is a mid-execution instruction, not a retry. Raises
        `RunNotSteerableError` when nothing is in flight to receive it.
        """
        ...

    async def transition(self, run_id: RunId, node_id: NodeId) -> RunSnapshot:
        """Move control to `node_id` by hand and run on from there.

        The graph's own state is rewound to what it was when that node was last
        entered, so sending a run back to `implementation` resumes the work
        rather than replaying a later node's conclusions into it. Raises
        `UnknownNodeError` when the graph has no such node.
        """
        ...

    async def decide(
        self, run_id: RunId, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> RunSnapshot:
        """Answer what a node stopped to ask, and let it go on.

        The snapshot is the run as the decision left it -- released, or failed
        when the decision was to cancel -- and not however far the node then got.
        A reply that waited for the graph would mean something different every
        time depending on what the node did next; what happens after the release
        is what the event feed is for.

        Raises `UnknownApprovalError` for a request this run never raised, and
        `ApprovalNotPendingError` for one that is no longer waiting.
        """
        ...

    async def aclose(self) -> None:
        """Stop every run this runtime is driving.

        Part of the contract because a control surface is a server: something
        has to happen on SIGTERM, and an implementation whose runs were only
        ever background tasks would drop them mid-node with no chance to
        checkpoint. Idempotent -- shutdown may be reached more than once.
        """
        ...


__all__ = [
    "ApprovalNotPendingError",
    "GraphRuntime",
    "LangGraphRuntimeError",
    "PendingApproval",
    "RunNotSteerableError",
    "RunSnapshot",
    "RunStatus",
    "UnknownApprovalError",
    "UnknownGraphError",
    "UnknownNodeError",
    "UnknownRunError",
]

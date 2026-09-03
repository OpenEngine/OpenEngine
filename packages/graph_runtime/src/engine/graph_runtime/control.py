"""The contract the HTTP surface is written against.

`GraphRuntime` is everything the control server needs a graph to be able to do,
and nothing about how a graph does it. LangGraph is the intended implementation
and the tests drive a scripted one, which is the point of stating it as a
protocol: the server, its wire format, and its behaviour under steering and
resumption are all buildable and checkable before the binding exists, and the
same tests then run against the binding unchanged.

Three things here are deliberately shaped by what LangGraph actually is, because
getting them wrong would be expensive to undo:

* A run's position is a *frontier*, not a node. LangGraph's execution primitive
  is the superstep, and a superstep may run several nodes at once. There is no
  truthful value for "the current node" while three reviewers are working, so
  the snapshot does not offer one.
* What is in flight is an *execution*, not a node. A superstep can fan several
  tasks into the same node -- LangGraph's `Send` does exactly that -- so a node
  name does not identify one of them. Every in-flight thing has an
  `ExecutionId`, and that is what control is addressed to.
* A run is resumed from a *checkpoint*, not a node. "Send it back to
  implementation" is a selector the HTTP layer resolves; the primitive
  underneath is `resume_from`, which forks rather than rewrites. See
  `engine.graph_runtime.checkpoints`.

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

from engine.graph_runtime.checkpoints import Checkpoint, CheckpointId
from engine.graph_runtime.events import EventObserver
from engine.graph_runtime.identity import ActiveExecution, ExecutionId
from engine.graph_runtime.topology import GraphId, GraphTopology, NodeId


class RunStatus(Enum):
    """Where a run is, as far as anyone outside it can tell."""

    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    """At least one execution is waiting on a person.

    Not "the run has stopped": with a fan-out, one reviewer can be waiting for
    permission to run a command while the other two carry on working.
    `RunSnapshot.pending_approvals` is the precise answer; this is the summary
    a list view can show.
    """
    COMPLETED = "completed"
    FAILED = "failed"
    """Stopped without finishing, with `RunSnapshot.error` saying why.

    A node raising is the ordinary way here, not an exceptional one, so it is
    part of the contract rather than something a binding may leave undefined: an
    implementation must publish `run.failed`, naming the node that raised, and
    reach this status. A run whose task died silently would report `running`
    forever, hold a subscriber waiting for a terminal event that never came, and
    refuse steering as having nothing in flight -- three answers a client cannot
    reconcile, and no way to tell it apart from a node that is still thinking.
    """


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One thing an execution has stopped to ask for.

    Kept in fields rather than a rendered sentence, for the reason
    `engine.domain.approvals` keeps its record that way: a stored `command` can
    be compared and shown differently by a different client.

    `execution_id` is what routes the answer. The question was raised by whatever
    a node is driving -- an agent asking to run a command -- and the answer goes
    back to that exact execution, not into the graph and not to the node: with
    two reviewers running the same node, only one of them asked.

    `node_id` is alongside it for display, and is not the address.
    """

    approval_id: ApprovalId
    execution_id: ExecutionId
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

    There is no history here on purpose. A list of nodes visited reads well for
    a straight line and lies about everything else -- fan-out, loops, retries,
    subgraphs -- and it cannot say which attempt a node was entered on. What
    happened is `history()` plus the event feed, both of which keep the shape.
    """

    run_id: RunId
    graph_id: GraphId
    status: RunStatus
    active_executions: tuple[ActiveExecution, ...] = ()
    """What is executing right now, each with its own id.

    Plural because a superstep is: a reviewer pool is one step of three, and
    there is no honest single answer while all three are working. Empty while
    the run is stopped, finished, or between supersteps.

    Executions rather than nodes, and not both: two tasks fanned into the same
    node are two entries here, and a list of node names beside them could only
    say `review` once or say it twice without distinguishing them. A client that
    wants the frontier reads the node ids off these.
    """
    next_nodes: tuple[NodeId, ...] = ()
    """What the run would execute next.

    While a superstep is in flight, the frontier it will move to when that step
    commits. When nothing is executing -- failed, finished, waiting -- what a
    resume from `checkpoint_id` would run.
    """
    checkpoint_id: CheckpointId | None = None
    """The position this run is at, and the one a resume defaults to."""
    values: Mapping[str, object] = field(default_factory=dict)
    """The graph's state, as the graph itself would report it."""
    pending_approvals: tuple[PendingApproval, ...] = ()
    error: str = ""


class GraphCompilationError(Exception):
    """A graph this deployment was given cannot be built, and this says which.

    Deliberately *not* a `GraphRuntimeError`: those are refusals of a request
    somebody made, answerable with a status code. This is a deployment that
    cannot be assembled -- a graph in the workflow directory whose nodes and
    edges do not make a graph -- and there is nobody to answer, because it
    happens while the process is starting.

    It names the graph, because "a graph failed to compile" is not actionable
    in a directory holding several, and carries the original failure as its
    cause so a traceback still points at the line in the workflow file.
    """

    def __init__(self, graph_id: GraphId, cause: BaseException) -> None:
        super().__init__(f"graph workflow {str(graph_id)!r} does not compile: {cause}")
        self.graph_id = graph_id
        self.reason = str(cause)


class GraphRuntimeError(Exception):
    """Anything the control surface refuses, in one catchable family."""


class UnknownGraphError(GraphRuntimeError):
    """No graph by that id is registered."""


class UnknownRunError(GraphRuntimeError):
    """No run by that id, in this process or its store."""


class UnknownNodeError(GraphRuntimeError):
    """A request named a node the run's graph does not have."""


class UnknownCheckpointError(GraphRuntimeError):
    """No checkpoint by that id belongs to this run."""


class NoSuchPositionError(GraphRuntimeError):
    """The node exists, but no checkpoint has ever been about to run it.

    Different from `UnknownNodeError` on purpose: the client did not misspell
    anything, it asked to go back to somewhere the run has not been yet.
    """


class UnknownApprovalError(GraphRuntimeError):
    """No request by that id was raised by this run."""


class ApprovalNotPendingError(GraphRuntimeError):
    """The request outlived whatever was waiting on it, or was already answered."""


class RunNotSteerableError(GraphRuntimeError):
    """There is no execution in flight to deliver the message to."""


class AmbiguousExecutionError(GraphRuntimeError):
    """Several executions match, and the request did not name one of them.

    The cost of a truthful fan-out: with three reviewers running, "steer this
    run" has three answers, and picking one would be a guess about which agent
    the person was watching. A node name is not enough either once the same node
    is running twice -- which is why the answer names the executions.
    """


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

    async def history(self, run_id: RunId) -> tuple[Checkpoint, ...]:
        """Every checkpoint this run has, oldest first.

        Including the ones a fork replaced: nothing is ever removed, so an
        abandoned attempt stays readable beside the attempt that replaced it.
        Raises `UnknownRunError` for a run that does not exist.
        """
        ...

    async def resume_from(
        self, run_id: RunId, checkpoint_id: CheckpointId
    ) -> RunSnapshot:
        """Fork from `checkpoint_id` and execute forward.

        The control primitive underneath "send it back to implementation". The
        graph's state becomes what that checkpoint held, and the run continues
        into the frontier that checkpoint was about to execute -- but as a new
        checkpoint whose parent is the named one, so what is being re-attempted
        is still there.

        Anything in flight is stopped first, and keeping that promise is what
        makes two of these arriving at once safe. Stopping is asynchronous --
        something has to be cancelled and waited for -- so an implementation
        that let a second call run during it would have two executors driving
        one run, interleaving into one history and one state, and publishing two
        endings for it. Control operations on a run are therefore serialised: a
        second resume waits for the first and then forks from wherever that one
        left the run, rather than racing it.

        Raises `UnknownCheckpointError`.
        """
        ...

    async def steer(
        self,
        run_id: RunId,
        message: str,
        execution_id: ExecutionId | None = None,
        node_id: NodeId | None = None,
    ) -> RunSnapshot:
        """Deliver external input to the active execution.

        Routed to whatever is executing now -- for an agent node, the session
        already talking to Claude or Codex -- and not into the graph. The node
        is not interrupted, resumed or restarted by this; it is running, and a
        message for the thing it is running is not a question about what should
        run next. See `engine.graph_runtime.executions` for the boundary.

        `execution_id` is the address. `node_id` is shorthand a client may use
        when exactly one execution of that node is in flight, which is the
        common case and the one a person can name from a diagram.

        Raises `RunNotSteerableError` when nothing matches, and
        `AmbiguousExecutionError` when several do and none was named -- two
        tasks fanned into the same node make even a node name ambiguous.
        """
        ...

    async def decide(
        self, run_id: RunId, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> RunSnapshot:
        """Answer what an execution stopped to ask, and let it go on.

        Routed to the execution that raised the request -- which has been alive
        and waiting the whole time -- by the `execution_id` recorded on the
        request, never by its node. The graph node was never suspended, so
        nothing about it is resumed here.

        The snapshot is the run as the decision left it -- released, or over
        when the decision was to cancel -- and not however far the execution
        then got. A reply that waited for the graph would mean something
        different every time depending on what the agent did next; what happens
        after the release is what the event feed is for.

        A refusal is the exception, because it does not release anything: it
        ends the run, and the answer is not given until it has. The siblings of
        a refused request may be blocked on questions of their own that nobody
        will now answer, so a runtime that only set the status would publish no
        terminal event at all, leave those executions still taking steering and
        decisions, and report `failed` to one client while another was still
        driving the same run. `RunStatus.FAILED` exists to prevent exactly that
        pair of answers, so reaching it has to mean the run is really finished.

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
    "AmbiguousExecutionError",
    "ApprovalNotPendingError",
    "GraphRuntime",
    "GraphRuntimeError",
    "NoSuchPositionError",
    "PendingApproval",
    "RunNotSteerableError",
    "RunSnapshot",
    "RunStatus",
    "UnknownApprovalError",
    "UnknownCheckpointError",
    "UnknownGraphError",
    "UnknownNodeError",
    "UnknownRunError",
]

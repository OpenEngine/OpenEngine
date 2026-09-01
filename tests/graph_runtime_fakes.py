"""A scripted stand-in for the graph the control surface drives.

Not a mock of the control surface: a real `GraphRuntime`, with real asyncio
tasks, real supersteps that fan out, a real execution that waits for permission
nobody has given yet, and a real execution that takes an instruction while it is
still running. What it does not have is LangGraph, so what each node "decides"
to do is a script instead.

That is the point of the split. The HTTP surface, its wire format, and its
behaviour under steering and resumption are all decided here and checked in
`tests/test_graph_runtime.py`; when the LangGraph binding lands it satisfies the
same protocol and the same tests run against it unchanged.

The important thing this fake models -- and the reason it is not simply a stub
-- is where control goes. Each task in flight registers a `ControllableExecution`
under its own `ExecutionId` and keeps it registered for the whole task: while it
works, while it waits on an approval, and while somebody redirects it. `steer()`
and `decide()` are routed to that object and the node's coroutine never stops or
restarts, which is exactly the lifecycle an `ACPNode` holding an `ACPSession`
has. A fake that implemented approvals by tearing the node down and running it
again would pass a weaker suite and prove nothing about the binding.

`ScriptedNode.tasks` is how the same node runs more than once at a time, which
is what LangGraph's `Send` does. Two tasks of one node are two executions with
two queues and two sets of open questions, and the fake would be misleading if
they shared either.

A node is a tuple of beats. `Say` and `Call` are things it does; `Ask` is a
pause on consent, taken by the execution rather than by the graph;
`AwaitSteering` is a point where it waits for an instruction and then carries
on; `Fail` raises, which is the one thing a real node does that none of the
others can.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import count

from engine.domain import ApprovalDecision, ApprovalId, ApprovalKind, RunId
from engine.graph_runtime import (
    ActiveExecution,
    ApprovalNotPendingError,
    Checkpoint,
    CheckpointId,
    EventKind,
    EventObserver,
    ExecutionId,
    ExecutionRegistry,
    GraphEdge,
    GraphId,
    GraphNode,
    GraphTopology,
    NodeId,
    PendingApproval,
    RunSnapshot,
    RunStatus,
    RuntimeEvent,
    UnknownApprovalError,
    UnknownCheckpointError,
    UnknownGraphError,
    UnknownRunError,
)


@dataclass(frozen=True, slots=True)
class Say:
    """The node adds one message to its transcript."""

    text: str
    role: str = "assistant"


@dataclass(frozen=True, slots=True)
class Call:
    """The node calls a tool and is given `result` back."""

    name: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    result: str = "ok"


@dataclass(frozen=True, slots=True)
class Ask:
    """The execution stops and waits for consent, staying alive to be told."""

    reason: str
    command: str = ""
    tool_name: str = ""
    kind: ApprovalKind = ApprovalKind.COMMAND_EXECUTION


@dataclass(frozen=True, slots=True)
class AwaitSteering:
    """The execution stops until somebody sends it a message."""


@dataclass(frozen=True, slots=True)
class Fail:
    """The node raises.

    The likeliest thing a real node does that none of the other beats can: an
    agent whose provider is out of quota, a tool that is not installed, a bug.
    Scripted so the contract has to say what a run does about it, rather than
    leaving the answer to whichever binding hits it first.
    """

    message: str


class ScriptedFailure(RuntimeError):
    """What a `Fail` beat raises. Nothing catches it by type."""


Beat = Say | Call | Ask | AwaitSteering | Fail


@dataclass(frozen=True, slots=True)
class ScriptedNode:
    node_id: NodeId
    beats: tuple[Beat, ...] = ()
    next_nodes: tuple[NodeId, ...] = ()
    """Every successor, run together as one superstep. Plural on purpose."""
    tasks: int = 1
    """How many concurrent executions of this node one superstep starts.

    LangGraph's `Send` fans several tasks into one node -- the same reviewer
    prompt over three candidate diffs, say. Each is its own execution with its
    own id, its own approvals and its own transcript, and none of them can be
    addressed by the node name they share.
    """
    name: str = ""
    kind: str = "agent"


@dataclass(frozen=True, slots=True)
class ScriptedGraph:
    """A set of nodes. The first one is where a run begins."""

    graph_id: GraphId
    name: str
    nodes: tuple[ScriptedNode, ...]

    def node(self, node_id: NodeId) -> ScriptedNode | None:
        return next((node for node in self.nodes if node.node_id == node_id), None)

    def successors(self, frontier: Sequence[NodeId]) -> tuple[NodeId, ...]:
        """The next superstep: everything the frontier leads to, once each."""
        following: dict[NodeId, None] = {}
        for node_id in frontier:
            node = self.node(node_id)
            if node is not None:
                following.update(dict.fromkeys(node.next_nodes))
        return tuple(following)

    def topology(self) -> GraphTopology:
        return GraphTopology(
            graph_id=self.graph_id,
            name=self.name,
            entry_point=self.nodes[0].node_id,
            nodes=tuple(
                GraphNode(node.node_id, node.name or str(node.node_id), node.kind)
                for node in self.nodes
            ),
            edges=tuple(
                GraphEdge(node.node_id, target)
                for node in self.nodes
                for target in node.next_nodes
            ),
        )


@dataclass(frozen=True, slots=True)
class _Outcome:
    """How one node in a superstep ended."""

    node_id: NodeId
    error: str = ""
    refused: bool = False
    """An approval was answered "no", which ends the run rather than the node."""


class _Run:
    """One execution of one scripted graph."""

    def __init__(self, run_id: RunId, graph: ScriptedGraph) -> None:
        self.run_id = run_id
        self.graph = graph
        self.status = RunStatus.RUNNING
        self.values: dict[str, object] = {}
        self.frontier: tuple[NodeId, ...] = ()
        """The superstep in flight, which is what `next_nodes` looks past."""
        self.checkpoints: list[Checkpoint] = []
        self.by_id: dict[CheckpointId, Checkpoint] = {}
        self.pending: dict[ApprovalId, PendingApproval] = {}
        self.answered: set[ApprovalId] = set()
        """Requests that have been resolved, so a repeat is a 409 and not a 404."""
        self.error = ""
        self.task: asyncio.Task[None] | None = None

    @property
    def position(self) -> Checkpoint | None:
        return self.checkpoints[-1] if self.checkpoints else None

    def record(
        self,
        next_nodes: tuple[NodeId, ...],
        parent: CheckpointId | None,
        source: str,
        counter: count[int],
    ) -> Checkpoint:
        """Save a position. Appends -- a fork never replaces what it came from."""
        checkpoint = Checkpoint(
            checkpoint_id=CheckpointId(f"checkpoint-{next(counter)}"),
            parent_id=parent,
            next_nodes=next_nodes,
            values=dict(self.values),
            source=source,
        )
        self.checkpoints.append(checkpoint)
        self.by_id[checkpoint.checkpoint_id] = checkpoint
        return checkpoint

    def snapshot(self, active: tuple[ActiveExecution, ...]) -> RunSnapshot:
        position = self.position
        return RunSnapshot(
            run_id=self.run_id,
            graph_id=self.graph.graph_id,
            status=self._status(),
            active_executions=active,
            next_nodes=(
                self.graph.successors(self.frontier)
                if self.frontier
                else (position.next_nodes if position is not None else ())
            ),
            checkpoint_id=position.checkpoint_id if position is not None else None,
            values=dict(self.values),
            pending_approvals=tuple(self.pending.values()),
            error=self.error,
        )

    def _status(self) -> RunStatus:
        """Waiting on a person is a summary of the approvals, not a fourth state.

        Derived rather than assigned so a fan-out cannot contradict itself: with
        one reviewer waiting and two working, the run is both, and the honest
        answer is the one a list view can show plus the approvals underneath it.
        """
        if self.status is RunStatus.RUNNING and self.pending:
            return RunStatus.AWAITING_APPROVAL
        return self.status


class _Execution:
    """One task in flight, and the only thing external control reaches.

    Stands in for the session an agent node would be holding. It outlives every
    approval and every steering message: the node's coroutine is inside
    `ScriptedGraphRuntime._run_node` the whole time, and nothing here suspends
    or restarts it.

    One per task rather than per node: two tasks fanned into the same node are
    two of these, with two queues and two sets of open questions, so a message
    for one cannot be taken by the other.
    """

    def __init__(
        self,
        runtime: ScriptedGraphRuntime,
        run: _Run,
        execution_id: ExecutionId,
        node_id: NodeId,
    ) -> None:
        self._runtime = runtime
        self._run = run
        self.execution_id = execution_id
        self.node_id = node_id
        self._steering: asyncio.Queue[str] = asyncio.Queue()
        self._waiting: dict[ApprovalId, asyncio.Future[ApprovalDecision]] = {}

    # --- what the runtime routes to us -------------------------------------

    async def steer(self, message: str) -> None:
        self._steering.put_nowait(message)

    async def decide(
        self, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> None:
        waiting = self._waiting.get(approval_id)
        if waiting is None or waiting.done():
            raise ApprovalNotPendingError(
                f"approval is no longer pending: {approval_id}"
            )
        waiting.set_result(decision)

    # --- what the script does ----------------------------------------------

    async def ask(self, beat: Ask) -> ApprovalDecision:
        """Raise a request and wait, without going back to the graph."""
        approval = PendingApproval(
            approval_id=ApprovalId(f"approval-{next(self._runtime.ids)}"),
            execution_id=self.execution_id,
            node_id=self.node_id,
            kind=beat.kind,
            reason=beat.reason,
            command=beat.command,
            tool_name=beat.tool_name,
        )
        waiting: asyncio.Future[ApprovalDecision] = (
            asyncio.get_running_loop().create_future()
        )
        self._waiting[approval.approval_id] = waiting
        self._run.pending[approval.approval_id] = approval
        await self._runtime.emit(
            self._run,
            EventKind.APPROVAL_REQUESTED,
            {
                "approvalId": str(approval.approval_id),
                "kind": approval.kind.value,
                "reason": approval.reason,
                "command": approval.command,
                "toolName": approval.tool_name,
            },
            node_id=self.node_id,
            execution_id=self.execution_id,
        )
        try:
            decision = await waiting
        finally:
            # `decide` clears the run's copy on the way in; this is for the run
            # being stopped or forked while the question is still open.
            self._waiting.pop(approval.approval_id, None)
            self._run.pending.pop(approval.approval_id, None)
        await self._runtime.emit(
            self._run,
            EventKind.APPROVAL_RESOLVED,
            {"approvalId": str(approval.approval_id), "decision": decision.value},
            node_id=self.node_id,
            execution_id=self.execution_id,
        )
        return decision

    async def next_message(self) -> str:
        return await self._steering.get()

    async def drain(self) -> None:
        """Take whatever has arrived without waiting for more."""
        while not self._steering.empty():
            await self.receive(self._steering.get_nowait())

    async def receive(self, message: str) -> None:
        received = self._run.values.get("steering")
        self._run.values["steering"] = [
            *(received if isinstance(received, list) else ()),
            message,
        ]
        await self._runtime.emit(
            self._run,
            EventKind.TRANSCRIPT,
            {"role": "user", "text": message},
            node_id=self.node_id,
            execution_id=self.execution_id,
        )


class ScriptedGraphRuntime:
    """A `GraphRuntime` whose nodes follow a script instead of a model."""

    def __init__(self, *graphs: ScriptedGraph) -> None:
        self._graphs = {graph.graph_id: graph for graph in graphs}
        self._runs: dict[RunId, _Run] = {}
        self._observer: EventObserver | None = None
        self._executions = ExecutionRegistry()
        self._entries: dict[NodeId, int] = {}
        self.ids = count(1)

    # --- the control surface's contract ------------------------------------

    def observe(self, observer: EventObserver) -> None:
        self._observer = observer

    def graphs(self) -> tuple[GraphTopology, ...]:
        return tuple(graph.topology() for graph in self._graphs.values())

    def topology(self, graph_id: GraphId) -> GraphTopology | None:
        graph = self._graphs.get(graph_id)
        return graph.topology() if graph is not None else None

    async def start(
        self, graph_id: GraphId, values: Mapping[str, object]
    ) -> RunSnapshot:
        graph = self._graphs.get(graph_id)
        if graph is None:
            raise UnknownGraphError(f"unknown graph: {graph_id}")
        run = _Run(RunId(f"run-{next(self.ids)}"), graph)
        run.values.update(values)
        self._runs[run.run_id] = run
        await self.emit(run, EventKind.RUN_STARTED, {"values": dict(run.values)})
        opening = run.record(
            (graph.nodes[0].node_id,), parent=None, source="start", counter=self.ids
        )
        await self._emit_checkpoint(run, opening)
        run.task = asyncio.create_task(self._execute(run, opening))
        return self._snapshot(run)

    async def snapshot(self, run_id: RunId) -> RunSnapshot | None:
        run = self._runs.get(run_id)
        return self._snapshot(run) if run is not None else None

    async def history(self, run_id: RunId) -> tuple[Checkpoint, ...]:
        return tuple(self._require(run_id).checkpoints)

    async def resume_from(
        self, run_id: RunId, checkpoint_id: CheckpointId
    ) -> RunSnapshot:
        run = self._require(run_id)
        origin = run.by_id.get(checkpoint_id)
        if origin is None:
            raise UnknownCheckpointError(f"unknown checkpoint: {checkpoint_id}")
        await self._stop(run)
        # The state that position held, not whatever a later node concluded --
        # and appended as a child of it, so the attempt being replaced is still
        # in `history()` for anyone auditing how the run got here.
        run.values = dict(origin.values)
        run.error = ""
        run.status = RunStatus.RUNNING
        run.frontier = ()
        forked = run.record(
            origin.next_nodes,
            parent=checkpoint_id,
            source="fork",
            counter=self.ids,
        )
        await self.emit(
            run,
            EventKind.RUN_FORKED,
            {
                "from": str(checkpoint_id),
                "checkpointId": str(forked.checkpoint_id),
                "nodes": [str(node_id) for node_id in forked.next_nodes],
                "values": dict(run.values),
            },
        )
        # No await between here and the return, so the snapshot the caller is
        # answered with is the forked position rather than whatever the restarted
        # superstep has already got to.
        run.task = asyncio.create_task(self._execute(run, forked))
        return self._snapshot(run)

    async def steer(
        self,
        run_id: RunId,
        message: str,
        execution_id: ExecutionId | None = None,
        node_id: NodeId | None = None,
    ) -> RunSnapshot:
        run = self._require(run_id)
        target, execution = self._executions.resolve(run_id, execution_id, node_id)
        await execution.steer(message)
        # Accepted for delivery, not yet delivered: the execution picks it up at
        # its next interruption point and says so itself, with a transcript
        # entry. Blocking this call until then would mean an agent waiting on an
        # approval could never be sent an instruction.
        await self.emit(
            run,
            EventKind.STEERING_RECEIVED,
            {"message": message},
            node_id=target.node_id,
            execution_id=target.execution_id,
        )
        return self._snapshot(run)

    async def decide(
        self, run_id: RunId, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> RunSnapshot:
        run = self._require(run_id)
        approval = run.pending.get(approval_id)
        if approval is None:
            if approval_id in run.answered:
                raise ApprovalNotPendingError(
                    f"approval is no longer pending: {approval_id}"
                )
            raise UnknownApprovalError(f"unknown approval: {approval_id}")
        # Resolved before anything is mutated, so a request that cannot be
        # delivered does not leave the run half-answered. By execution rather
        # than by node: the other two reviewers did not ask this question.
        _, execution = self._executions.resolve(run_id, approval.execution_id)
        run.pending.pop(approval_id)
        run.answered.add(approval_id)
        if decision is ApprovalDecision.CANCEL:
            run.status = RunStatus.FAILED
            run.error = f"{approval.reason} was not allowed"
        await execution.decide(approval_id, decision)
        return self._snapshot(run)

    async def aclose(self) -> None:
        """Stop every run, as the server's shutdown does."""
        for run in self._runs.values():
            await self._stop(run)

    # --- what a test can ask that a client cannot --------------------------

    def running(self) -> tuple[_Run, ...]:
        """The runs still executing, which shutdown has to leave empty.

        Not part of `GraphRuntime`: "is a task still alive" is an
        implementation's own question, and the HTTP surface has no way to ask
        it -- a leaked task looks exactly like a node that is thinking.
        """
        return tuple(
            run
            for run in self._runs.values()
            if run.task is not None and not run.task.done()
        )

    def entered(self, node_id: NodeId) -> int:
        """How many times a node has been started, across every attempt.

        The question "did that restart the node?" reduces to, and a fake that
        could not answer it would let a binding satisfy the steering contract by
        replaying the node from the top.
        """
        return self._entries.get(node_id, 0)

    # --- execution ---------------------------------------------------------

    async def _execute(self, run: _Run, checkpoint: Checkpoint) -> None:
        """Drive the run, and make sure a node that raises is reported as one.

        Without this, the task would die with its exception unretrieved and the
        run would still claim to be running -- forever, to every client asking.
        """
        try:
            await self._walk(run, checkpoint)
        except asyncio.CancelledError:
            raise
        except Exception as failure:  # pragma: no cover - a bug in the fake
            await self._fail(run, str(failure), None)

    async def _walk(self, run: _Run, checkpoint: Checkpoint) -> None:
        while checkpoint.next_nodes:
            run.frontier = checkpoint.next_nodes
            # One superstep: every node in the frontier at once, and every task
            # of each of them, which is what makes a reviewer pool one step
            # rather than three -- and one node's three tasks one step too.
            outcomes = await asyncio.gather(
                *(
                    self._run_node(run, node_id)
                    for node_id in checkpoint.next_nodes
                    for _ in range(self._tasks(run, node_id))
                )
            )
            run.frontier = ()
            raised = next((one for one in outcomes if one.error), None)
            if raised is not None:
                await self._fail(run, raised.error, raised.node_id)
                return
            refused = next((one for one in outcomes if one.refused), None)
            if refused is not None:
                await self._fail(run, run.error, refused.node_id)
                return
            checkpoint = run.record(
                run.graph.successors(checkpoint.next_nodes),
                parent=checkpoint.checkpoint_id,
                source="superstep",
                counter=self.ids,
            )
            await self._emit_checkpoint(run, checkpoint)
        run.status = RunStatus.COMPLETED
        await self.emit(run, EventKind.RUN_FINISHED, {"values": dict(run.values)})

    def _tasks(self, run: _Run, node_id: NodeId) -> int:
        node = run.graph.node(node_id)
        return node.tasks if node is not None else 1

    async def _run_node(self, run: _Run, node_id: NodeId) -> _Outcome:
        node = run.graph.node(node_id)
        assert node is not None
        execution = _Execution(
            self, run, ExecutionId(f"execution-{next(self.ids)}"), node_id
        )
        self._entries[node_id] = self._entries.get(node_id, 0) + 1
        # Registered for the whole execution, so steering and approvals reach it
        # wherever it has got to -- and released however it ends, including by
        # the cancellation a fork does.
        with self._executions.in_flight(
            run.run_id, execution.execution_id, node_id, execution
        ):
            await self.emit(
                run,
                EventKind.NODE_STARTED,
                node_id=node_id,
                execution_id=execution.execution_id,
            )
            try:
                for beat in node.beats:
                    await execution.drain()
                    if not await self._play(run, execution, beat):
                        return _Outcome(node_id, refused=True)
            except asyncio.CancelledError:
                raise
            except Exception as failure:
                return _Outcome(node_id, error=str(failure))
            await self.emit(
                run,
                EventKind.NODE_FINISHED,
                {"values": dict(run.values)},
                node_id=node_id,
                execution_id=execution.execution_id,
            )
        return _Outcome(node_id)

    async def _play(self, run: _Run, execution: _Execution, beat: Beat) -> bool:
        """Play one beat. False means the run ended here."""
        node_id = execution.node_id
        execution_id = execution.execution_id
        match beat:
            case Say(text=text, role=role):
                run.values[str(node_id)] = text
                await self.emit(
                    run,
                    EventKind.TRANSCRIPT,
                    {"role": role, "text": text},
                    node_id=node_id,
                    execution_id=execution_id,
                )
            case Call(name=name, arguments=arguments, result=result):
                call_id = f"call-{next(self.ids)}"
                await self.emit(
                    run,
                    EventKind.TOOL_CALL,
                    {"callId": call_id, "name": name, "arguments": dict(arguments)},
                    node_id=node_id,
                    execution_id=execution_id,
                )
                await self.emit(
                    run,
                    EventKind.TOOL_RESULT,
                    {"callId": call_id, "name": name, "result": result},
                    node_id=node_id,
                    execution_id=execution_id,
                )
            case Ask():
                return await execution.ask(beat) is not ApprovalDecision.CANCEL
            case AwaitSteering():
                await execution.receive(await execution.next_message())
            case Fail(message=message):
                raise ScriptedFailure(message)
        return True

    async def _fail(self, run: _Run, error: str, node_id: NodeId | None) -> None:
        run.status = RunStatus.FAILED
        run.error = error
        await self.emit(run, EventKind.RUN_FAILED, {"error": error}, node_id=node_id)

    async def _stop(self, run: _Run) -> None:
        run.pending.clear()
        task = run.task
        run.task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for active in self._executions.active(run.run_id):
            self._executions.release(run.run_id, active.execution_id)
        run.frontier = ()

    def _require(self, run_id: RunId) -> _Run:
        run = self._runs.get(run_id)
        if run is None:
            raise UnknownRunError(f"unknown run: {run_id}")
        return run

    def _snapshot(self, run: _Run) -> RunSnapshot:
        # The registry is the source of truth for what is executing: it is the
        # same thing steering resolves against, so a snapshot cannot promise a
        # node a message could not actually reach.
        return run.snapshot(self._executions.active(run.run_id))

    async def _emit_checkpoint(self, run: _Run, checkpoint: Checkpoint) -> None:
        await self.emit(
            run,
            EventKind.CHECKPOINT,
            {
                "checkpointId": str(checkpoint.checkpoint_id),
                "parentId": (
                    str(checkpoint.parent_id) if checkpoint.parent_id else None
                ),
                "nextNodes": [str(node_id) for node_id in checkpoint.next_nodes],
                "source": checkpoint.source,
                "values": dict(checkpoint.values),
            },
        )

    async def emit(
        self,
        run: _Run,
        kind: EventKind,
        payload: Mapping[str, object] | None = None,
        node_id: NodeId | None = None,
        execution_id: ExecutionId | None = None,
    ) -> None:
        if self._observer is None:
            return
        await self._observer(
            RuntimeEvent(
                run_id=run.run_id,
                kind=kind,
                payload=dict(payload or {}),
                node_id=node_id,
                execution_id=execution_id,
            )
        )


__all__ = [
    "Ask",
    "AwaitSteering",
    "Beat",
    "Call",
    "Fail",
    "Say",
    "ScriptedFailure",
    "ScriptedGraph",
    "ScriptedGraphRuntime",
    "ScriptedNode",
]

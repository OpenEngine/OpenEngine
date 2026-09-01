"""A scripted stand-in for the graph the control surface drives.

Not a mock of the control surface: a real `GraphRuntime`, with real asyncio
tasks, a real pause on an approval nobody has answered yet, and a real node that
blocks mid-execution waiting to be steered. What it does not have is LangGraph,
so what each node "decides" to do is a script instead.

That is the point of the split. The HTTP surface, its wire format, and its
behaviour under steering and manual transition are all decided here and checked
in `tests/test_langgraph_runtime.py`; when the LangGraph binding lands it
satisfies the same protocol and the same tests run against it unchanged.

A node is a tuple of beats. `Say` and `Call` are things it does; `Ask` is a
pause on consent; `AwaitSteering` is the interruption point steering exists for
-- the node stops there, and continues with the beats after it once a message
arrives, rather than starting the node again; `Fail` raises, which is the one
thing a real node does that none of the others can.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import count

from engine.domain import ApprovalDecision, ApprovalId, ApprovalKind, RunId
from engine.langgraph_runtime import (
    ApprovalNotPendingError,
    EventKind,
    EventObserver,
    GraphEdge,
    GraphId,
    GraphNode,
    GraphTopology,
    NodeId,
    PendingApproval,
    RunNotSteerableError,
    RunSnapshot,
    RunStatus,
    RuntimeEvent,
    UnknownApprovalError,
    UnknownGraphError,
    UnknownNodeError,
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
    """The node stops and waits for consent."""

    reason: str
    command: str = ""
    tool_name: str = ""
    kind: ApprovalKind = ApprovalKind.COMMAND_EXECUTION


@dataclass(frozen=True, slots=True)
class AwaitSteering:
    """The node stops until somebody sends it a message."""


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
    next_node: NodeId | None = None
    name: str = ""
    kind: str = "agent"


@dataclass(frozen=True, slots=True)
class ScriptedGraph:
    """A straight line of nodes. The first one is where a run begins."""

    graph_id: GraphId
    name: str
    nodes: tuple[ScriptedNode, ...]

    def node(self, node_id: NodeId) -> ScriptedNode | None:
        return next((node for node in self.nodes if node.node_id == node_id), None)

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
                GraphEdge(node.node_id, node.next_node)
                for node in self.nodes
                if node.next_node is not None
            ),
        )


class _Run:
    """One execution of one scripted graph."""

    def __init__(self, run_id: RunId, graph: ScriptedGraph) -> None:
        self.run_id = run_id
        self.graph = graph
        self.status = RunStatus.RUNNING
        self.current_node: NodeId | None = graph.nodes[0].node_id
        self.visited: list[NodeId] = []
        self.values: dict[str, object] = {}
        self.entry_values: dict[NodeId, dict[str, object]] = {}
        """The state each node was entered with, which is what a rewind restores."""
        self.pending: dict[
            ApprovalId, tuple[PendingApproval, asyncio.Future[ApprovalDecision]]
        ] = {}
        self.answered: set[ApprovalId] = set()
        """Requests that have been resolved, so a repeat is a 409 and not a 404."""
        self.steering: asyncio.Queue[str] = asyncio.Queue()
        self.error = ""
        self.task: asyncio.Task[None] | None = None

    def snapshot(self) -> RunSnapshot:
        return RunSnapshot(
            run_id=self.run_id,
            graph_id=self.graph.graph_id,
            status=self.status,
            current_node=self.current_node,
            visited=tuple(self.visited),
            values=dict(self.values),
            pending_approvals=tuple(
                approval for approval, _ in self.pending.values()
            ),
            error=self.error,
        )


class ScriptedLangGraph:
    """A `GraphRuntime` whose nodes follow a script instead of a model."""

    def __init__(self, *graphs: ScriptedGraph) -> None:
        self._graphs = {graph.graph_id: graph for graph in graphs}
        self._runs: dict[RunId, _Run] = {}
        self._observer: EventObserver | None = None
        self._ids = count(1)

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
        run = _Run(RunId(f"run-{next(self._ids)}"), graph)
        run.values.update(values)
        self._runs[run.run_id] = run
        await self._emit(run, EventKind.RUN_STARTED, {"values": dict(run.values)})
        run.task = asyncio.create_task(self._execute(run, graph.nodes[0].node_id))
        return run.snapshot()

    async def snapshot(self, run_id: RunId) -> RunSnapshot | None:
        run = self._runs.get(run_id)
        return run.snapshot() if run is not None else None

    async def steer(self, run_id: RunId, message: str) -> RunSnapshot:
        run = self._require(run_id)
        if run.task is None or run.task.done() or run.current_node is None:
            raise RunNotSteerableError("this run has no node in flight")
        run.steering.put_nowait(message)
        # Accepted for delivery, not yet delivered: the node picks it up at its
        # next interruption point and says so itself, with a transcript entry.
        # Blocking this call until then would mean a node paused on an approval
        # could never be sent an instruction.
        await self._emit(
            run,
            EventKind.STEERING_RECEIVED,
            {"message": message},
            node_id=run.current_node,
        )
        return run.snapshot()

    async def transition(self, run_id: RunId, node_id: NodeId) -> RunSnapshot:
        run = self._require(run_id)
        if run.graph.node(node_id) is None:
            raise UnknownNodeError(f"unknown node: {node_id}")
        previous = run.current_node
        await self._stop(run)
        # Rewound to the state the node was entered with, so a run sent back to
        # implementation resumes the work rather than reading a later node's
        # conclusions as its own input. A node that has never run has nothing
        # recorded, and keeps the state the run has now.
        run.values = dict(run.entry_values.get(node_id, run.values))
        if node_id in run.visited:
            run.visited = run.visited[: run.visited.index(node_id)]
        run.error = ""
        run.status = RunStatus.RUNNING
        run.current_node = node_id
        await self._emit(
            run,
            EventKind.TRANSITION,
            {
                "from": str(previous) if previous else None,
                "to": str(node_id),
                "values": dict(run.values),
            },
            node_id=node_id,
        )
        # No await between here and the return, so the snapshot the caller is
        # answered with is the rewound one rather than whatever the restarted
        # node has already got to.
        run.task = asyncio.create_task(self._execute(run, node_id))
        return run.snapshot()

    async def decide(
        self, run_id: RunId, approval_id: ApprovalId, decision: ApprovalDecision
    ) -> RunSnapshot:
        run = self._require(run_id)
        found = run.pending.get(approval_id)
        if found is None:
            if approval_id in run.answered:
                raise ApprovalNotPendingError(
                    f"approval is no longer pending: {approval_id}"
                )
            raise UnknownApprovalError(f"unknown approval: {approval_id}")
        approval, future = found
        # The decision's own effect is applied here, before the node is woken:
        # the request is answered, and the run is either released or over. What
        # the node does next belongs to the node, and waiting for it would make
        # this reply mean "wherever the graph happened to get to" -- `running`,
        # `completed` or `failed` depending on the script, and different again
        # the next time the same request is answered.
        run.pending.pop(approval_id)
        run.answered.add(approval_id)
        if decision is ApprovalDecision.CANCEL:
            run.status = RunStatus.FAILED
            run.error = f"{approval.reason} was not allowed"
        else:
            run.status = RunStatus.RUNNING
        future.set_result(decision)
        return run.snapshot()

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

    # --- execution ---------------------------------------------------------

    async def _execute(self, run: _Run, start_node: NodeId) -> None:
        """Drive the run, and make sure a node that raises is reported as one.

        Without this, the task would die with its exception unretrieved and the
        run would still claim to be running -- forever, to every client asking.
        """
        try:
            await self._walk(run, start_node)
        except asyncio.CancelledError:
            raise
        except Exception as failure:
            run.status = RunStatus.FAILED
            run.error = str(failure)
            await self._emit(run, EventKind.RUN_FAILED, {"error": run.error})

    async def _walk(self, run: _Run, start_node: NodeId) -> None:
        current: NodeId | None = start_node
        while current is not None:
            node = run.graph.node(current)
            assert node is not None
            run.current_node = current
            run.visited.append(current)
            run.entry_values[current] = dict(run.values)
            await self._emit(run, EventKind.NODE_STARTED, node_id=current)
            for beat in node.beats:
                await self._drain_steering(run, current)
                if not await self._play(run, current, beat):
                    return
            await self._emit(
                run,
                EventKind.NODE_FINISHED,
                {"values": dict(run.values)},
                node_id=current,
            )
            current = node.next_node
        run.current_node = None
        run.status = RunStatus.COMPLETED
        await self._emit(run, EventKind.RUN_FINISHED, {"values": dict(run.values)})

    async def _play(self, run: _Run, node_id: NodeId, beat: Beat) -> bool:
        """Play one beat. False means the run ended here."""
        match beat:
            case Say(text=text, role=role):
                run.values[str(node_id)] = text
                await self._emit(
                    run,
                    EventKind.TRANSCRIPT,
                    {"role": role, "text": text},
                    node_id=node_id,
                )
            case Call(name=name, arguments=arguments, result=result):
                call_id = f"call-{next(self._ids)}"
                await self._emit(
                    run,
                    EventKind.TOOL_CALL,
                    {"callId": call_id, "name": name, "arguments": dict(arguments)},
                    node_id=node_id,
                )
                await self._emit(
                    run,
                    EventKind.TOOL_RESULT,
                    {"callId": call_id, "name": name, "result": result},
                    node_id=node_id,
                )
            case Ask():
                return await self._ask(run, node_id, beat)
            case AwaitSteering():
                await self._receive(run, node_id, await run.steering.get())
            case Fail(message=message):
                raise ScriptedFailure(message)
        return True

    async def _ask(self, run: _Run, node_id: NodeId, beat: Ask) -> bool:
        approval = PendingApproval(
            approval_id=ApprovalId(f"approval-{next(self._ids)}"),
            node_id=node_id,
            kind=beat.kind,
            reason=beat.reason,
            command=beat.command,
            tool_name=beat.tool_name,
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalDecision] = loop.create_future()
        run.pending[approval.approval_id] = (approval, future)
        run.status = RunStatus.AWAITING_APPROVAL
        await self._emit(
            run,
            EventKind.APPROVAL_REQUESTED,
            {
                "approvalId": str(approval.approval_id),
                "kind": approval.kind.value,
                "reason": approval.reason,
                "command": approval.command,
                "toolName": approval.tool_name,
            },
            node_id=node_id,
        )
        try:
            decision = await future
        finally:
            # `decide` clears these on the way in; this is for the run being
            # stopped or sent elsewhere while the question is still open.
            run.pending.pop(approval.approval_id, None)
        # The status the decision left behind is already the run's -- what is
        # left is to say so on the feed, and to stop if the answer was no.
        await self._emit(
            run,
            EventKind.APPROVAL_RESOLVED,
            {
                "approvalId": str(approval.approval_id),
                "decision": decision.value,
            },
            node_id=node_id,
        )
        if decision is ApprovalDecision.CANCEL:
            run.current_node = node_id
            await self._emit(run, EventKind.RUN_FAILED, {"error": run.error})
            return False
        return True

    async def _drain_steering(self, run: _Run, node_id: NodeId) -> None:
        """Take whatever has been sent without waiting for more."""
        while not run.steering.empty():
            await self._receive(run, node_id, run.steering.get_nowait())

    async def _receive(self, run: _Run, node_id: NodeId, message: str) -> None:
        received = run.values.get("steering")
        run.values["steering"] = [
            *(received if isinstance(received, list) else ()),
            message,
        ]
        await self._emit(
            run,
            EventKind.TRANSCRIPT,
            {"role": "user", "text": message},
            node_id=node_id,
        )

    async def _stop(self, run: _Run) -> None:
        for _, future in run.pending.values():
            if not future.done():
                future.cancel()
        run.pending.clear()
        task = run.task
        run.task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def _require(self, run_id: RunId) -> _Run:
        run = self._runs.get(run_id)
        if run is None:
            raise UnknownRunError(f"unknown run: {run_id}")
        return run

    async def _emit(
        self,
        run: _Run,
        kind: EventKind,
        payload: Mapping[str, object] | None = None,
        node_id: NodeId | None = None,
    ) -> None:
        if self._observer is None:
            return
        await self._observer(
            RuntimeEvent(
                run_id=run.run_id,
                kind=kind,
                payload=dict(payload or {}),
                node_id=node_id,
            )
        )


__all__ = [
    "Ask",
    "AwaitSteering",
    "Call",
    "Fail",
    "Say",
    "ScriptedFailure",
    "ScriptedGraph",
    "ScriptedLangGraph",
    "ScriptedNode",
]

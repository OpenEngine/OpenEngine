"""One graph description, two runtimes to run it on.

`tests/test_graph_runtime.py` decides what the control surface means, and every
test in it is written against a `ScriptedGraph` -- a tuple of beats per node.
This module turns one of those into either implementation of the contract:

    ScriptedGraph  --+--> ScriptedGraphRuntime      (tests/graph_runtime_fakes)
                     |
                     +--> LangGraphRuntime          (a real compiled LangGraph)

so the same suite runs against both and neither gets a weaker one. That is the
whole point of parameterizing rather than writing a second, kinder set of tests
for the binding: the fake was built to state the contract, and a binding that
only passed tests written after it would prove nothing about the contract.

The LangGraph side is a real graph. `Say` and `Call` are things a node does,
`Ask` raises an approval the execution waits on without the graph being
interrupted, `AwaitSteering` waits on the queue steering arrives at, and `Fail`
raises. `ScriptedNode.tasks` becomes `Send`, which is how LangGraph fans several
concurrent tasks into one node -- the shape that makes a node name useless as an
address.

Two differences between the backends are real rather than incidental, and the
suite is written to respect them:

* LangGraph's state changes at superstep boundaries, not while a node runs. A
  scripted node's `Say` is visible to a snapshot immediately; a LangGraph node's
  is visible once the node returns. `Backend.commits_mid_node` says which.
* A LangGraph checkpoint id is LangGraph's, so nothing may assume the shape of
  one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import count
from typing import Annotated, Any, Protocol

from engine.domain import ApprovalDecision
from engine.graph_runtime import GraphRuntime, NodeId
from engine.graph_runtime_langgraph import (
    LangGraphDefinition,
    LangGraphRuntime,
    current_execution,
)
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from graph_runtime_fakes import (
    Ask,
    AwaitSteering,
    Beat,
    Call,
    Fail,
    Say,
    ScriptedFailure,
    ScriptedGraph,
    ScriptedGraphRuntime,
    ScriptedNode,
)


class Backend(Protocol):
    """How a test builds the runtime it is about to drive."""

    name: str
    commits_mid_node: bool
    """Whether a snapshot taken while a node runs sees what it has produced."""

    def __call__(self, *graphs: ScriptedGraph) -> GraphRuntime: ...


def _merge(current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    """The graph's state, with lists appended rather than replaced.

    A dict rather than a `TypedDict` because a run's values are whatever the
    client started it with -- `{"repository": "acme/api"}` -- and a schema with
    named fields could not hold them. Lists append because `steering` is a
    record of everything an execution was told, and the last message is not the
    whole of it.
    """
    merged = dict(current or {})
    for key, value in (incoming or {}).items():
        held = merged.get(key)
        if isinstance(value, list) and isinstance(held, list):
            merged[key] = [*held, *value]
        else:
            merged[key] = value
    return merged


State = Annotated[dict[str, Any], _merge]


@dataclass(frozen=True, slots=True)
class _Player:
    """A LangGraph node that plays one scripted node's beats.

    Everything it does goes through `current_execution()`, which is the same
    handle steering and approvals are routed to -- so a message sent while this
    is waiting reaches *this* task, and not the other two running the same node.
    """

    beats: tuple[Beat, ...]
    graph_node_kind: str = "agent"
    graph_node_description: str = ""

    async def __call__(self, state: Mapping[str, Any]) -> dict[str, Any]:
        execution = current_execution()
        produced: dict[str, Any] = {}
        heard: list[str] = []
        counter = count(1)
        for beat in self.beats:
            for message in execution.pending_messages():
                heard.append(message)
                await execution.say(message, role="user")
            match beat:
                case Say(text=text, role=role):
                    produced[str(execution.node_id)] = text
                    await execution.say(text, role=role)
                case Call(name=name, arguments=arguments, result=result):
                    await execution.tool(
                        f"call-{next(counter)}", name, arguments, result
                    )
                case Ask():
                    decision = await execution.ask(
                        reason=beat.reason,
                        kind=beat.kind,
                        command=beat.command,
                        tool_name=beat.tool_name,
                    )
                    if decision is ApprovalDecision.CANCEL:
                        # The runtime has already stopped the run; this only
                        # matters if it somehow has not, and a node that carried
                        # on after being refused would be worse than one that
                        # stops loudly.
                        raise ScriptedFailure(f"{beat.reason} was not allowed")
                case AwaitSteering():
                    message = await execution.next_message()
                    heard.append(message)
                    await execution.say(message, role="user")
                case Fail(message=text):
                    raise ScriptedFailure(text)
        if heard:
            produced["steering"] = heard
        return produced


def compile_scripted(
    scripted: ScriptedGraph, checkpointer: Any | None = None
) -> LangGraphDefinition:
    """One scripted graph as a real compiled LangGraph.

    Fan-out is edges: a node with three successors is one superstep of three.
    Repetition is `Send`: a successor declaring `tasks=3` is one node run three
    times over, concurrently, with three ids -- which is a different shape and
    the reason both are in the suite.
    """
    builder: StateGraph = StateGraph(State)
    for node in scripted.nodes:
        builder.add_node(
            str(node.node_id), _Player(node.beats, graph_node_kind=node.kind)
        )
    builder.add_edge(START, str(scripted.nodes[0].node_id))
    for node in scripted.nodes:
        _connect(builder, scripted, node)
    return LangGraphDefinition(
        graph_id=scripted.graph_id,
        name=scripted.name,
        graph=builder.compile(checkpointer=checkpointer or InMemorySaver()),
        names={
            str(node.node_id): node.name or str(node.node_id)
            for node in scripted.nodes
        },
    )


def _connect(
    builder: StateGraph, scripted: ScriptedGraph, node: ScriptedNode
) -> None:
    if not node.next_nodes:
        builder.add_edge(str(node.node_id), END)
        return
    fanned = tuple(
        target
        for target in node.next_nodes
        if (found := scripted.node(target)) is not None and found.tasks > 1
    )
    if fanned:
        builder.add_conditional_edges(
            str(node.node_id),
            _sender(scripted, node.next_nodes),
            [str(target) for target in node.next_nodes],
        )
        return
    for target in node.next_nodes:
        builder.add_edge(str(node.node_id), str(target))


def _sender(scripted: ScriptedGraph, targets: Sequence[NodeId]):
    """The branch that turns `tasks=n` into n concurrent tasks of one node."""

    def send(_state: Mapping[str, Any]) -> list[Send]:
        fanned: list[Send] = []
        for target in targets:
            found = scripted.node(target)
            for _ in range(found.tasks if found is not None else 1):
                fanned.append(Send(str(target), {}))
        return fanned

    return send


@dataclass(frozen=True, slots=True)
class ScriptedBackend:
    """The stand-in the contract was written against."""

    name: str = "scripted"
    commits_mid_node: bool = True

    def __call__(self, *graphs: ScriptedGraph) -> GraphRuntime:
        return ScriptedGraphRuntime(*graphs)


@dataclass(frozen=True, slots=True)
class LangGraphBackend:
    """The same graphs, compiled and run by LangGraph."""

    name: str = "langgraph"
    commits_mid_node: bool = False

    def __call__(self, *graphs: ScriptedGraph) -> GraphRuntime:
        # One saver for every graph in the runtime, as a deployment has: threads
        # are keyed by run, and two savers would make a run's history depend on
        # which graph happened to be asked for it.
        saver = InMemorySaver()
        return LangGraphRuntime(
            *(compile_scripted(graph, saver) for graph in graphs)
        )


BACKENDS: tuple[Backend, ...] = (ScriptedBackend(), LangGraphBackend())


async def settle() -> None:
    """Give every runnable task a turn. Used where a test has to see quiet."""
    for _ in range(3):
        await asyncio.sleep(0)


__all__ = [
    "BACKENDS",
    "Backend",
    "LangGraphBackend",
    "ScriptedBackend",
    "State",
    "compile_scripted",
    "settle",
]

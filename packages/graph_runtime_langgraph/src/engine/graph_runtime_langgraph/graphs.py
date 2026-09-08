"""A compiled LangGraph, and the description a client can be shown of it.

Topology is read off the graph rather than declared beside it. LangGraph already
knows its own nodes and edges -- `CompiledStateGraph.get_graph()` is what draws
the diagrams -- and a second, hand-written description would be the one that
goes stale the first time a conditional edge is added.

Two of LangGraph's nodes are not nodes a person can be sent to. `__start__` and
`__end__` are how a Pregel graph spells "the input channel" and "there is
nowhere left to go"; they have no agent, no transcript and no position in a
diagram, so they are left out of `nodes` and their edges are translated:

    __start__ -> implementation      becomes    entry_point = implementation
    review    -> __end__             becomes    (nothing)

What is *not* translated away is the frontier a checkpoint reports. A run
standing at its opening position is about to run the entry node, and that is
what `next_nodes` says; see `engine.graph_runtime_langgraph.runtime`.

`kind` comes from the node itself when it knows -- an agent node says `"agent"`
-- and is `"node"` otherwise. A control surface that guessed would be inventing
a fact about somebody else's graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Protocol

from engine.graph_runtime.topology import (
    GraphEdge,
    GraphId,
    GraphNode,
    GraphTopology,
    NodeId,
)

#: LangGraph's own endpoints. Real Pregel nodes, but not places a run goes.
START = "__start__"
END = "__end__"


class DescribesItself(Protocol):
    """A node that can say what to call it, what kind of thing it is, and why.

    Structural rather than a base class, and read with `getattr` rather than
    `isinstance`: a LangGraph node is any callable at all, and requiring one to
    inherit from something here would make the description available only to
    nodes written against this package. A node that says nothing is a `"node"`
    with no description, which is the truthful answer for somebody else's graph.

    A reusable node knows its own display name -- a checkout step is called
    "Workspace" in every graph that has one -- so the name comes from the node
    first and from the graph's `names` override second. That ordering is what
    keeps a workflow file from restating, in a second table, what the nodes it
    just assembled already say about themselves.
    """

    @property
    def graph_node_name(self) -> str: ...

    @property
    def graph_node_kind(self) -> str: ...

    @property
    def graph_node_description(self) -> str: ...

    @property
    def graph_node_show_in_sidebar(self) -> bool: ...
    """Whether a client's list of this run's conversations should offer it.

    True unless the node says otherwise, which is the same reasoning as the
    name: the node knows -- a checkout has no conversation to open in any
    graph that has one -- and a workflow file should not have to repeat it.
    """


@dataclass(frozen=True)
class LangGraphDefinition:
    """One compiled graph, under the id a client starts runs of.

    The graph must already be compiled with a checkpointer. That is not a
    convenience check: every position this runtime can report, resume or fork is
    a LangGraph checkpoint, so a graph compiled without one would satisfy
    `start()` and then have no history, no `resume_from`, and no way to be picked
    back up after a restart.
    """

    graph_id: GraphId
    name: str
    graph: Any
    """A `CompiledStateGraph`. Typed loosely so this module imports no Pregel."""
    names: dict[str, str] = field(default_factory=dict)
    """Display names per node id, overriding what a node calls itself."""

    def __post_init__(self) -> None:
        if getattr(self.graph, "checkpointer", None) is None:
            raise ValueError(
                f"graph {self.graph_id!r} was compiled without a checkpointer: "
                "checkpoints, history and resumption are LangGraph's, and a "
                "runtime cannot supply them on its behalf"
            )

    @cached_property
    def topology(self) -> GraphTopology:
        """The graph as a client is shown it.

        Computed once. A compiled graph's shape does not change, and a snapshot
        consults this to say what a running superstep will move to next -- so
        redrawing it per request would put a graph traversal in the path of
        every read of every run.
        """
        drawn = self.graph.get_graph()
        edges = tuple(
            GraphEdge(
                source=NodeId(edge.source),
                target=NodeId(edge.target),
                condition=edge.data or "" if edge.conditional else "",
            )
            for edge in drawn.edges
            if edge.source != START and edge.target != END
        )
        return GraphTopology(
            graph_id=self.graph_id,
            name=self.name,
            entry_point=self.entry_points[0],
            nodes=tuple(
                GraphNode(
                    node_id=NodeId(node.id),
                    name=self.names.get(node.id) or _name_of(node) or node.id,
                    kind=_kind_of(node),
                    description=_description_of(node),
                    show_in_sidebar=_shown_in_sidebar(node),
                )
                for node in drawn.nodes.values()
                if node.id not in (START, END)
            ),
            edges=edges,
        )

    @cached_property
    def entry_points(self) -> tuple[NodeId, ...]:
        """What `__start__` triggers: where a run actually begins.

        Plural because a graph may fan out of its entry, and ordered as the
        graph declares it so the first is the one a topology names.
        """
        drawn = self.graph.get_graph()
        found = tuple(
            NodeId(edge.target) for edge in drawn.edges if edge.source == START
        )
        if not found:
            raise ValueError(
                f"graph {self.graph_id!r} has no edge out of {START}: nothing "
                "would ever run"
            )
        return found


def _described(node: Any) -> Any:
    """The callable a drawn node stands for.

    LangGraph wraps a node in a `RunnableCallable`, which keeps a sync callable
    on `func` and an async one on `afunc`. Unwrapping both is what lets a node
    describe itself; the wrapper cannot, and asking it would describe every node
    in every graph identically.
    """
    wrapper = getattr(node, "data", None)
    return (
        getattr(wrapper, "afunc", None) or getattr(wrapper, "func", None) or wrapper
    )


def _name_of(node: Any) -> str:
    return str(getattr(_described(node), "graph_node_name", ""))


def _kind_of(node: Any) -> str:
    return str(getattr(_described(node), "graph_node_kind", "node"))


def _description_of(node: Any) -> str:
    return str(getattr(_described(node), "graph_node_description", ""))


def _shown_in_sidebar(node: Any) -> bool:
    """Whether a node belongs in a client's list of this run's conversations.

    Shown unless the node says otherwise: a graph this package did not write
    says nothing, and leaving its nodes out of the one navigation a person has
    would hide the run from them.
    """
    return bool(getattr(_described(node), "graph_node_show_in_sidebar", True))


__all__ = ["END", "START", "DescribesItself", "LangGraphDefinition"]

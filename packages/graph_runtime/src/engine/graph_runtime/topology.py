"""What a graph is, described without saying how it runs.

Topology is the part of a workflow a client can be shown before anything has
happened: the nodes that exist, how control can move between them, and where a
run starts. It is deliberately a description rather than a handle -- the same
shape is answerable by a compiled LangGraph, by a definition read off disk, and
by the scripted stand-in the tests drive, which is what lets the HTTP surface be
built and checked before any of them exists.

Nothing here says a node has one successor. A reviewer pool is several edges out
of one node, and the runtime executes them together as one superstep; see
`engine.graph_runtime.checkpoints`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NewType, Protocol, runtime_checkable

GraphId = NewType("GraphId", str)
"""A graph definition: "implementation-review", "triage"."""

NodeId = NewType("NodeId", str)
"""One node within a graph: "implementation", "review"."""


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One node, named as the client will be shown it."""

    node_id: NodeId
    name: str
    kind: str = "agent"
    """What running this node means: "agent", "tool", "human", "end".

    A string rather than an enum: LangGraph does not constrain what a node is,
    and a control surface that refused an unfamiliar kind would be unable to
    describe graphs it can otherwise drive perfectly well.
    """
    description: str = ""
    show_in_sidebar: bool = True
    """Whether a client listing this run's conversations should offer this node.

    True by default, because a node is normally somewhere a person can go and
    read what happened. A node says otherwise about itself -- the checkout that
    holds no conversation, the decision that is the reader's own -- so that a
    navigation built from a graph does not have to keep a list of the nodes it
    should leave out.

    Advice to a client rather than a permission: the node is still in the
    topology, still has a position, and is still readable at its own address.
    """


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """A way control can move from one node to another.

    `condition` is the branch's name when the edge is conditional, and empty
    when it is not. Which branch a run will actually take is not knowable from
    the topology, so it is not claimed here -- and neither is whether the edges
    out of a node are alternatives or a fan-out. Both are the run's business.
    """

    source: NodeId
    target: NodeId
    condition: str = ""


@dataclass(frozen=True, slots=True)
class GraphTopology:
    """Every node and edge of one graph, plus where a run begins."""

    graph_id: GraphId
    name: str
    entry_point: NodeId
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = field(default_factory=tuple)
    """Every edge, in the graph's own declaration order.

    Ordered rather than a set, and required to be stable: a client rendering a
    diagram twice should get the same picture, and one diffing two versions of a
    graph should see only what changed. An implementation that iterated a set
    would make both of those flicker for no reason a reader could see.

    Stable is not sorted. The order is whatever the graph declares, which is
    usually the order a person wrote the nodes in and is more useful than
    alphabetical; nothing here promises otherwise.
    """

    def node(self, node_id: NodeId) -> GraphNode | None:
        return next((node for node in self.nodes if node.node_id == node_id), None)


@runtime_checkable
class GraphWorkflow(Protocol):
    """A graph a deployment offers, before anything has compiled it.

    Two attributes, so that whatever loads a repository's workflow files can
    tell "this one is a graph" from "this one is steps" without importing a
    graph engine. Everything else about a graph belongs to the binding that
    runs it.

    Structural rather than a base class: a workflow file already imports the
    binding it was written against, and should not have to import this package
    as well just to be recognised.
    """

    @property
    def graph_id(self) -> GraphId: ...

    @property
    def name(self) -> str: ...


__all__ = [
    "GraphEdge",
    "GraphId",
    "GraphNode",
    "GraphTopology",
    "GraphWorkflow",
    "NodeId",
]

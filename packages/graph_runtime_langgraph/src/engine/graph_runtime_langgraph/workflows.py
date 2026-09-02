"""Writing a graph down, and turning what was written into a runtime.

A repository owns its workflows. What it should have to write is a graph and the
nodes in it -- nothing about checkpointers, stores, HTTP or process lifetimes,
because those are the same in every deployment and getting them subtly wrong is
the deployment's problem rather than the workflow's. So the boundary is:

    a workflow file      nodes, edges, an id and a name        `graph_workflow`
    a deployment         where state lives, and for how long   `sqlite_runtime`

`graph_workflow` produces a value, not a running thing. It holds an *uncompiled*
builder, because compiling needs a checkpointer and a checkpointer is a file
somebody has to own and close -- a decision a workflow module has no way to make
and no business making. `sqlite_runtime` is the other half: it opens the two
files, compiles every workflow against them, and closes both afterwards.

    workflow.py  ->  GraphWorkflow  --+
    workflow.py  ->  GraphWorkflow  --+->  sqlite_runtime(...)  ->  GraphRuntime

Which is also what will make a *variant* cheap: a graph is a value a module
exports, so a second deployment -- or a test -- runs a different one by being
handed different values, rather than by a flag whose two settings both have to
keep working. Nothing here loads a module or decides which graphs a process
offers; that is the workflow loader's, and arrives with the change that teaches
it to recognise one of these.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any, overload

from engine.graph_runtime import GraphId
from engine.graph_runtime_langgraph.acp import answer_permission
from engine.graph_runtime_langgraph.graphs import LangGraphDefinition
from engine.graph_runtime_langgraph.runtime import LangGraphRuntime
from engine.graph_runtime_langgraph.store import SqliteGraphRuntimeStore
from langgraph_acp import ACPAgentRegistry

#: LangGraph's checkpoints, and the runtime's own records. Two files because
#: they answer to two different owners: one is Pregel's idea of where a run
#: stands, the other is which approvals are outstanding and which ACP session
#: belongs to which node.
CHECKPOINTS = "checkpoints.sqlite3"
RUNS = "graph-runs.sqlite3"


def merge(current: dict[str, Any] | None, incoming: dict[str, Any] | None):
    """Last write wins, per key.

    A superstep's updates are merged into what was there rather than replacing
    it. Without this a node that reports only its own key would erase the
    checkout every later node needs -- and with fan-out, two nodes returning at
    once would race to be the whole state.
    """
    merged = dict(current or {})
    merged.update(incoming or {})
    return merged


State = Annotated[dict[str, Any], merge]
"""The state channel a workflow graph uses unless it has a reason not to.

An open mapping rather than a declared schema, because what a run carries is the
workflow's business: a task, a checkout, whatever each node reported. Nodes read
keys they know and leave the rest alone.
"""


@dataclass(frozen=True)
class GraphWorkflow:
    """One graph a deployment can be asked to run, before it is compiled.

    An id and a name, and then the LangGraph part. The first two are deliberately
    what a value like this leads with: whatever eventually decides which graphs a
    process offers has to be able to identify one without knowing that a builder,
    a checkpointer or a compilation step exist. Only `compiled` looks past them.
    """

    graph_id: GraphId
    name: str
    builder: Any
    """An uncompiled `StateGraph`. Typed loosely so this module imports no Pregel."""
    names: Mapping[str, str] = field(default_factory=dict)
    """Display names per node id, for a node that does not name itself."""

    def compiled(self, checkpointer: Any) -> LangGraphDefinition:
        """This graph, compiled against the checkpointer a deployment owns."""
        return LangGraphDefinition(
            graph_id=self.graph_id,
            name=self.name,
            graph=self.builder.compile(checkpointer=checkpointer),
            names=dict(self.names),
        )


@overload
def graph_workflow(
    builder: Any, *, id: str, name: str, names: Mapping[str, str] | None = None
) -> GraphWorkflow: ...


@overload
def graph_workflow(
    builder: None = None,
    *,
    id: str,
    name: str,
    names: Mapping[str, str] | None = None,
) -> Callable[[Callable[[], Any]], GraphWorkflow]: ...


def graph_workflow(
    builder: Any = None,
    *,
    id: str,
    name: str,
    names: Mapping[str, str] | None = None,
) -> GraphWorkflow | Callable[[Callable[[], Any]], GraphWorkflow]:
    """Name a graph, so a deployment can be asked to run it.

    Two spellings, because workflow files come in two shapes. A file describing
    one graph reads best as a decorator on the function that builds it:

        @graph_workflow(id="triage", name="Triage")
        def workflow():
            builder = StateGraph(State)
            ...
            return builder

    A file describing a *family* -- the same pipeline on each of several agents
    -- has one builder function and several names for it, and a decorator would
    force the body to be written out once per variant:

        workflow = tuple(
            graph_workflow(_pipeline(runner), id=f"review-{runner}", name=...)
            for runner in ("codex", "claude")
        )

    Both produce the same value. Node display names are normally the nodes' own
    (`graph_node_name`); `names` is the override for a node that has none.
    """
    if builder is None:

        def decorate(build: Callable[[], Any]) -> GraphWorkflow:
            return _workflow(build(), id=id, name=name, names=names)

        return decorate
    return _workflow(builder, id=id, name=name, names=names)


def _workflow(
    builder: Any, *, id: str, name: str, names: Mapping[str, str] | None
) -> GraphWorkflow:
    if not id.strip():
        raise ValueError("a graph workflow needs an id")
    if not name.strip():
        raise ValueError(f"graph workflow {id!r} needs a name")
    if not hasattr(builder, "compile"):
        raise TypeError(
            f"graph workflow {id!r} must be built from a StateGraph, not "
            f"{type(builder).__name__}"
        )
    return GraphWorkflow(
        graph_id=GraphId(id.strip()),
        name=name.strip(),
        builder=builder,
        names=dict(names or {}),
    )


def agent_registry(providers: Iterable[Any]) -> ACPAgentRegistry:
    """Register ACP agents so their permission requests reach the run.

    The one piece of wiring a workflow file would otherwise have to know about
    and would silently get wrong: a `session/request_permission` arrives on the
    ACP connection, not on the graph, and a provider left with the default
    handler *declines every request* rather than asking anybody. Attaching
    `answer_permission` is what routes the question to the execution holding the
    conversation, so it is done here rather than remembered per provider.
    """
    return ACPAgentRegistry(
        [replace(provider, permissions=answer_permission) for provider in providers]
    )


@asynccontextmanager
async def sqlite_runtime(
    workflows: Sequence[GraphWorkflow], directory: str | Path
) -> AsyncIterator[LangGraphRuntime]:
    """Every workflow, compiled against durable files, closed on the way out.

    The lifetime is the point. A checkpointer is an async context manager and a
    store is an open connection, so composing them without also owning their
    shutdown leaves SQLite handles open past the process that made them -- which
    is why this is a context manager and why nothing above it holds either file.
    """
    if not workflows:
        raise ValueError("a graph runtime needs at least one workflow")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    # Imported here rather than at module scope: the SQLite checkpointer is what
    # a *deployment* needs, and a test driving the contract against an in-memory
    # saver should not have to have it installed to import this module.
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async with AsyncSqliteSaver.from_conn_string(str(root / CHECKPOINTS)) as saver:
        store = SqliteGraphRuntimeStore(root / RUNS)
        runtime = LangGraphRuntime(
            *(workflow.compiled(saver) for workflow in workflows), store=store
        )
        try:
            yield runtime
        finally:
            await runtime.aclose()
            store.close()


__all__ = [
    "CHECKPOINTS",
    "RUNS",
    "GraphWorkflow",
    "State",
    "agent_registry",
    "graph_workflow",
    "merge",
    "sqlite_runtime",
]

"""Nodes a workflow can use rather than write.

A *component* is a LangGraph node with the graph runtime's conventions already
in it: it describes itself for the topology a client is shown, it publishes what
it does as runtime events, and where it has to stop for a person it stops the
way the control surface expects. A repository writing a workflow assembles
these; it should not have to reimplement any of them, and two workflows that did
would disagree about what a checkout or a human decision means.

    WorkspaceNode      give this run somewhere to work
    HumanReviewNode    stop, and wait for a person
    ACPNode            run a coding agent over ACP

Each is an ordinary async callable and each takes its collaborators as
arguments, so nothing here reaches for a global or asks its process what it is
being deployed as. `WorkspaceNode` takes a `WorkspaceProvider` -- the port,
not any particular one of them -- which is what keeps a git worktree out of a
package that must not name an adapter.

That last property is load-bearing rather than tidiness, and `checkout` and
`ACPNode.cwd` are where it is enforced: a component that fell back on its
process would fall back on the *server's* checkout, and an agent given edit
permission there would edit the operator's repository with nothing in the run
saying so. So neither has a default and neither has a `None` to hand on. See
`NoWorkingDirectoryError`.

`ACPNode` is re-exported rather than moved. It is inseparable from the session,
permission and process-handoff machinery in `engine.graph_runtime_langgraph.acp`
and reads as one piece with it; a graph author still gets it from here, which is
the only thing the split would have bought.
"""

from engine.graph_runtime_langgraph.acp import ACPNode, NoWorkingDirectoryError
from engine.graph_runtime_langgraph.components.human_review import HumanReviewNode
from engine.graph_runtime_langgraph.components.workspace import (
    WorkspaceNode,
    checkout,
)

__all__ = [
    "ACPNode",
    "HumanReviewNode",
    "NoWorkingDirectoryError",
    "WorkspaceNode",
    "checkout",
]

"""Give a run somewhere to work.

A node rather than something `start()` does, and that is the whole design
decision here: provisioning a checkout clones, fetches and writes to disk, so it
fails the way real work fails. As a node it is a position a run stands at, is
watched arriving at, and -- when the base ref does not exist or the disk is full
-- is reported as having failed at, with the same event feed carrying the news
as for every other node. Done inside `start()` it would instead be a request
that hangs and then 500s, with no run to look at afterwards.

The path is written into the graph's state rather than kept here, because the
nodes that need it are the agent nodes downstream:

    WorkspaceNode        ->  state["workspace"]  ->  ACPNode(cwd=checkout)

which is what lets one compiled graph serve every run. The provider is a
`WorkspaceProvider` -- the capability, not any particular implementation -- so a
deployment backed by git worktrees, containers or anything else assembles the
same workflow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from engine.graph_runtime_langgraph.acp import NoWorkingDirectoryError
from engine.graph_runtime_langgraph.executions import current_execution
from engine.ports import WorkspaceProvider

#: Where the checkout's path lands, and what an agent node reads for its `cwd`.
CHECKOUT = "workspace"

#: Where the run's repository is read from, when the node is not told one.
REPOSITORY = "repository"


@dataclass(frozen=True, slots=True)
class WorkspaceNode:
    """Provision a checkout for this run and put it in the graph's state."""

    provider: WorkspaceProvider
    base_ref: str = "origin/main"
    """What the checkout is based on. A ref the provider can resolve."""
    repository: str = ""
    """The repository to check out, or empty to take it from the run's values.

    Empty by default because the repository is usually the run's rather than the
    workflow's: one graph serves every WorkOrder, and each names what it is
    about to change when it starts.
    """

    graph_node_name: str = "Workspace"
    graph_node_kind: str = "workspace"
    graph_node_description: str = "Checks the repository out for this run."
    graph_node_show_in_sidebar: bool = False
    """Not a conversation, so it is not offered as one.

    A checkout says what it did -- the run's page prints the directory, and the
    provisioning is a tool call on the run's feed -- but there is nobody here to
    talk to. The conversations a rail offers are the agents'.
    """

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        execution = current_execution()
        repository = self.repository or str(state.get(REPOSITORY) or ".")
        await execution.say(f"Checking {repository} out at {self.base_ref}.")
        workspace = await self.provider.provision(repository, self.base_ref)
        # Checked here rather than left for whoever reads the state, so the
        # complaint names the provider that answered rather than the node three
        # steps later that could not work anywhere. A provider is somebody
        # else's code and an empty path is a plausible thing for one to return.
        if not workspace.root_path.strip():
            raise NoWorkingDirectoryError(
                f"{type(self.provider).__name__} provisioned a workspace with no "
                f"path for {repository!r}."
            )
        # Reported as a tool call, not just as prose: a checkout is a thing that
        # happened to the world, and a client showing the run should be able to
        # render it beside the agent's own calls rather than parse a sentence.
        await execution.tool(
            "provision",
            "provision_workspace",
            {"repository": repository, "baseRef": self.base_ref},
            workspace.root_path,
        )
        return {
            CHECKOUT: workspace.root_path,
            "workspaceRef": workspace.ref,
            "workspaceId": str(workspace.workspace_id),
        }


def checkout(state: Mapping[str, object]) -> str:
    """The checkout a `WorkspaceNode` upstream provisioned.

    Written to be handed straight to an agent node -- `ACPNode(cwd=checkout)` --
    so that "work where this run's workspace is" is one word rather than a
    lambda every workflow writes slightly differently.

    Raises `NoWorkingDirectoryError` when this run has no checkout, rather than
    answering `None`. The answer matters more than it looks: this value's
    destination is a working directory, and ACP reads a missing one as the
    client's own process, so a `None` returned from here would put an agent in
    the server's checkout instead of the run's. There is no sense in which "no
    workspace" is a directory to work in, so it is not one this function can
    return.
    """
    path = state.get(CHECKOUT)
    if not isinstance(path, str) or not path.strip():
        raise NoWorkingDirectoryError(
            "this run has no checkout to work in. A `WorkspaceNode` provisions "
            f"one into state[{CHECKOUT!r}], and has to run before whichever "
            "node reads it."
        )
    return path


__all__ = [
    "CHECKOUT",
    "REPOSITORY",
    "NoWorkingDirectoryError",
    "WorkspaceNode",
    "checkout",
]

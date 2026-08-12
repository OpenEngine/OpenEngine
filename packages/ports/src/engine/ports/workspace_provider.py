"""Workspace Provider capability.

Hands out isolated, disposable filesystems for agents to work in -- a git
worktree, a container, or a remote sandbox. The engine only ever holds a
`WorkspaceId`; it never touches a path itself.

A conversation outlives its checkout. Directories are cheap and expendable --
they get removed, swept out of /tmp, lost to a reboot -- while the work done in
one is not. So a workspace has two separable parts: the *work*, which the id
refers to for as long as the conversation exists, and the *checkout*, which can
be released with `detach` and brought back with `attach` without the id ever
changing. `dispose` is the one that ends both.
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from engine.domain.ids import WorkspaceId


@dataclass(frozen=True, slots=True)
class Workspace:
    """A provisioned, ready-to-use checkout."""

    workspace_id: WorkspaceId
    root_path: str
    repository: str
    base_ref: str
    ref: str = ""
    """Where this workspace's work lives -- what a human checks out to read it.

    A branch, a tag, a commit: whatever the provider's notion of "this
    workspace's history" is. Survives `detach`, so it stays the answer to
    "what did the agent do?" when there is no checkout to look in.
    """


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """What a provider knows about a workspace, checkout or no checkout."""

    workspace_id: WorkspaceId
    ref: str
    root_path: str | None = None
    """The checkout directory, or None when the workspace is detached."""

    @property
    def attached(self) -> bool:
        return self.root_path is not None


@runtime_checkable
class WorkspaceProvider(Protocol):
    """Creates and destroys isolated working environments."""

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        """Mint a new workspace, with a checkout ready to work in."""
        ...

    async def root_path(self, workspace_id: WorkspaceId) -> str:
        """Resolve an opaque workspace id to its checkout directory.

        Raises `KeyError` when the workspace is detached. Callers that are
        about to *run* something want this: a missing directory is an error,
        not a default. Callers that only want to describe a workspace want
        `state`.
        """
        ...

    async def state(self, workspace_id: WorkspaceId) -> WorkspaceState:
        """Describe the workspace without requiring it to be attached."""
        ...

    async def attach(
        self, workspace_id: WorkspaceId, repository: str, base_ref: str
    ) -> Workspace:
        """Give this workspace a checkout again, carrying its work back in.

        Idempotent -- an attached workspace is returned as it stands. A
        workspace whose work is gone (or which never had any) is checked out
        fresh at `base_ref` under the same id, so the caller's reference to it
        stays valid either way.
        """
        ...

    async def detach(self, workspace_id: WorkspaceId) -> None:
        """Release the checkout, keeping the work. Idempotent."""
        ...

    async def dispose(self, workspace_id: WorkspaceId) -> None:
        """Release the workspace and the work in it. Idempotent."""
        ...


__all__ = ["Workspace", "WorkspaceProvider", "WorkspaceState"]

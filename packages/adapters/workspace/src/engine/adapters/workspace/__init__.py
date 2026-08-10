"""Workspace Provider capability, backed by local git worktrees.

Placeholder for Ticket 1. Satisfies `engine.ports.WorkspaceProvider`
structurally; no cloning, worktree management, or cleanup yet.
"""

from engine.domain.ids import WorkspaceId
from engine.ports.workspace_provider import Workspace


class GitWorktreeWorkspaceProvider:
    """Provisions isolated checkouts as git worktrees under a root directory.

    Implements `engine.ports.WorkspaceProvider`.
    """

    def __init__(self, root_directory: str) -> None:
        self._root_directory = root_directory

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        raise NotImplementedError("Worktree provisioning lands with the workspace ticket")

    async def dispose(self, workspace_id: WorkspaceId) -> None:
        raise NotImplementedError("Worktree cleanup lands with the workspace ticket")


__all__ = ["GitWorktreeWorkspaceProvider"]

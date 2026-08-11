"""Workspace Provider capability.

Hands out isolated, disposable filesystems for agents to work in -- a git
worktree, a container, or a remote sandbox. The engine only ever holds a
`WorkspaceId`; it never touches a path itself.
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


@runtime_checkable
class WorkspaceProvider(Protocol):
    """Creates and destroys isolated working environments."""

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        ...

    async def root_path(self, workspace_id: WorkspaceId) -> str:
        """Resolve an opaque workspace id to its checkout directory."""
        ...

    async def dispose(self, workspace_id: WorkspaceId) -> None:
        """Release the workspace. Idempotent -- safe to call twice."""
        ...


__all__ = ["Workspace", "WorkspaceProvider"]

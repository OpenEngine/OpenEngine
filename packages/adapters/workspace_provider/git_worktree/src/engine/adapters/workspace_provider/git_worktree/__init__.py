"""Workspace Provider capability, backed by local git worktrees."""

import asyncio
from pathlib import Path
from uuid import uuid4

from engine.domain.ids import WorkspaceId
from engine.ports.workspace_provider import Workspace


class GitWorktreeWorkspaceProvider:
    """Provisions isolated checkouts as git worktrees under a root directory.

    Implements `engine.ports.WorkspaceProvider`.
    """

    def __init__(self, root_directory: str) -> None:
        self._root_directory = Path(root_directory).resolve()

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        repository_root = await _git(repository, "rev-parse", "--show-toplevel")
        workspace_id = WorkspaceId(f"ws-{uuid4().hex[:12]}")
        root_path = self._path_for(workspace_id)
        self._root_directory.mkdir(parents=True, exist_ok=True)
        await _git(
            repository_root,
            "worktree",
            "add",
            "-b",
            f"engine/{workspace_id}",
            str(root_path),
            base_ref,
        )
        return Workspace(
            workspace_id=workspace_id,
            root_path=str(root_path),
            repository=repository_root,
            base_ref=base_ref,
        )

    async def root_path(self, workspace_id: WorkspaceId) -> str:
        root_path = self._path_for(workspace_id)
        if not root_path.is_dir():
            raise KeyError(f"no workspace {workspace_id!r}")
        return str(root_path)

    async def dispose(self, workspace_id: WorkspaceId) -> None:
        root_path = self._path_for(workspace_id)
        if not root_path.exists():
            return
        await _git(str(root_path), "worktree", "remove", "--force", str(root_path))

    def _path_for(self, workspace_id: WorkspaceId) -> Path:
        name = str(workspace_id)
        if not name or Path(name).name != name:
            raise ValueError(f"invalid workspace id {workspace_id!r}")
        return self._root_directory / name


class GitWorktreeError(RuntimeError):
    """Git could not create or manage a workspace."""


async def _git(repository: str, *arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repository,
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise GitWorktreeError(detail or f"git exited {process.returncode}")
    return stdout.decode(errors="replace").strip()


__all__ = ["GitWorktreeError", "GitWorktreeWorkspaceProvider"]

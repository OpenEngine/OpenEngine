"""Local Git worktree workspace provisioning."""

import asyncio
from pathlib import Path
import subprocess

import pytest

from engine.adapters.workspace_provider.git_worktree import (
    GitWorktreeError,
    GitWorktreeWorkspaceProvider,
)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("engine\n")
    _git(path, "add", "README.md")
    _git(
        path,
        "-c",
        "user.name=Engine Tests",
        "-c",
        "user.email=engine@example.test",
        "commit",
        "-m",
        "initial",
    )


def test_each_workspace_is_a_distinct_worktree(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    first = asyncio.run(provider.provision(str(repository), "HEAD"))
    second = asyncio.run(provider.provision(str(repository), "HEAD"))

    assert first.workspace_id != second.workspace_id
    assert first.root_path != second.root_path
    assert Path(first.root_path, "README.md").read_text() == "engine\n"
    assert _git(Path(first.root_path), "branch", "--show-current") == (
        f"engine/{first.workspace_id}"
    )
    assert asyncio.run(provider.root_path(first.workspace_id)) == first.root_path


def test_dispose_is_idempotent(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))

    asyncio.run(provider.dispose(workspace.workspace_id))
    asyncio.run(provider.dispose(workspace.workspace_id))

    assert not Path(workspace.root_path).exists()


def test_a_non_repository_is_reported_as_a_workspace_error(tmp_path: Path) -> None:
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    with pytest.raises(GitWorktreeError):
        asyncio.run(provider.provision(str(tmp_path), "HEAD"))

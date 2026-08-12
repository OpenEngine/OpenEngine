"""Local Git worktree workspace provisioning."""

import asyncio
from pathlib import Path
import shutil
import subprocess

import pytest

from engine.adapters.workspace_provider.git_worktree import (
    BranchInUseError,
    GitWorktreeError,
    GitWorktreeWorkspaceProvider,
)


_IDENTITY = ("-c", "user.name=Engine Tests", "-c", "user.email=engine@example.test")


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
    _git(path, *_IDENTITY, "commit", "-m", "initial")


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


def test_dispose_takes_the_work_with_it(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))

    asyncio.run(provider.dispose(workspace.workspace_id))
    asyncio.run(provider.dispose(workspace.workspace_id))

    assert not Path(workspace.root_path).exists()
    assert workspace.ref not in _git(repository, "branch", "--list", workspace.ref)


def test_detaching_keeps_the_branch_and_reattaching_restores_the_work(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    Path(workspace.root_path, "agent.md").write_text("what the agent did\n")

    asyncio.run(provider.detach(workspace.workspace_id))
    detached = asyncio.run(provider.state(workspace.workspace_id))
    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert not detached.attached
    assert detached.root_path is None
    # Uncommitted work is the normal state of an agent's worktree; detaching
    # snapshots it onto the branch rather than throwing it away.
    assert detached.ref == workspace.ref
    assert "agent.md" in _git(repository, "show", "--name-only", detached.ref)
    assert reattached.workspace_id == workspace.workspace_id
    assert reattached.root_path == workspace.root_path
    assert Path(reattached.root_path, "agent.md").read_text() == "what the agent did\n"
    assert _git(Path(reattached.root_path), "branch", "--show-current") == workspace.ref


def test_detach_is_idempotent_and_leaves_committed_work_alone(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    root_path = Path(workspace.root_path)
    (root_path / "agent.md").write_text("committed by the agent\n")
    _git(root_path, "add", "agent.md")
    _git(root_path, *_IDENTITY, "commit", "-m", "the agent's own commit")
    committed = _git(root_path, "rev-parse", "HEAD")

    asyncio.run(provider.detach(workspace.workspace_id))
    asyncio.run(provider.detach(workspace.workspace_id))

    assert not root_path.exists()
    assert _git(repository, "rev-parse", workspace.ref) == committed


def test_work_is_snapshotted_even_where_git_has_no_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A machine that has never run `git config user.email` still detaches."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-system"))
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    Path(workspace.root_path, "agent.md").write_text("what the agent did\n")

    asyncio.run(provider.detach(workspace.workspace_id))

    assert "agent.md" in _git(repository, "show", "--name-only", workspace.ref)


def test_reattaching_a_branch_someone_is_reading_says_where_it_went(
    tmp_path: Path,
) -> None:
    """Reviewing the work is the point of the branch, so say how to hand it back."""
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    asyncio.run(provider.detach(workspace.workspace_id))
    _git(repository, "switch", workspace.ref)

    with pytest.raises(BranchInUseError) as refusal:
        asyncio.run(provider.attach(workspace.workspace_id, str(repository), "HEAD"))

    assert refusal.value.ref == workspace.ref
    assert refusal.value.checkout == str(repository)
    assert str(refusal.value).splitlines()[1] == (
        f"hint: switch that checkout to another branch first via "
        f"`git -C {repository} switch -`"
    )
    # Refused, not half-done: the checkout it could not make is not left behind.
    assert not Path(workspace.root_path).exists()


def test_the_branch_a_workspace_is_already_on_is_not_in_use_by_someone_else(
    tmp_path: Path,
) -> None:
    """The workspace's own checkout must not read as a stranger holding it."""
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))

    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert reattached.root_path == workspace.root_path


def test_attach_replaces_a_checkout_deleted_behind_gits_back(tmp_path: Path) -> None:
    """A swept /tmp leaves an administrative entry that would refuse a new one."""
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    shutil.rmtree(workspace.root_path)

    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert reattached.root_path == workspace.root_path
    assert Path(reattached.root_path, "README.md").read_text() == "engine\n"


def test_attach_checks_out_afresh_when_even_the_branch_is_gone(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    asyncio.run(provider.dispose(workspace.workspace_id))

    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert reattached.workspace_id == workspace.workspace_id
    assert Path(reattached.root_path, "README.md").read_text() == "engine\n"


def test_attach_is_idempotent(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _repository(repository)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))
    workspace = asyncio.run(provider.provision(str(repository), "HEAD"))
    Path(workspace.root_path, "scratch.txt").write_text("mid-turn\n")

    reattached = asyncio.run(
        provider.attach(workspace.workspace_id, str(repository), "HEAD")
    )

    assert reattached.root_path == workspace.root_path
    # An attached workspace is left exactly as it stands, work in progress and all.
    assert Path(workspace.root_path, "scratch.txt").read_text() == "mid-turn\n"


def test_a_non_repository_is_reported_as_a_workspace_error(tmp_path: Path) -> None:
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "worktrees"))

    with pytest.raises(GitWorktreeError):
        asyncio.run(provider.provision(str(tmp_path), "HEAD"))

"""Workspace Provider capability, backed by local git worktrees.

Every workspace owns a branch, `engine/<workspace id>`, and that branch -- not
the directory under the worktree root -- is what the workspace *is*. Detaching
removes the checkout and leaves the branch; attaching checks the branch out
again at the same path. The pair survives the things that happen to a directory
in /tmp, and it means the agent's work is always one `git checkout` away from a
human, whether or not a worktree is currently sitting on it.
"""

import asyncio
from pathlib import Path
from uuid import uuid4

from engine.domain.ids import WorkspaceId
from engine.ports.workspace_provider import Workspace, WorkspaceState

#: Used only when the repository has no committer identity of its own, so that
#: snapshotting an agent's work cannot fail on an unconfigured machine.
FALLBACK_IDENTITY = ("engine", "engine@localhost")

#: What `detach` records when it finds work that was never committed.
SNAPSHOT_MESSAGE = "engine: snapshot of uncommitted work before detaching"


class GitWorktreeWorkspaceProvider:
    """Provisions isolated checkouts as git worktrees under a root directory.

    Implements `engine.ports.WorkspaceProvider`.
    """

    def __init__(self, root_directory: str) -> None:
        self._root_directory = Path(root_directory).resolve()

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        workspace_id = WorkspaceId(f"ws-{uuid4().hex[:12]}")
        repository_root = await _repository_root(repository)
        resolved_base = await _resolve_base(repository_root, base_ref)
        return await self._checkout(
            workspace_id, repository_root, base_ref, resolved_base=resolved_base
        )

    async def root_path(self, workspace_id: WorkspaceId) -> str:
        root_path = self._path_for(workspace_id)
        if not root_path.is_dir():
            raise KeyError(f"no workspace {workspace_id!r}")
        return str(root_path)

    async def state(self, workspace_id: WorkspaceId) -> WorkspaceState:
        root_path = self._path_for(workspace_id)
        return WorkspaceState(
            workspace_id=workspace_id,
            ref=_branch_for(workspace_id),
            root_path=str(root_path) if root_path.is_dir() else None,
        )

    async def attach(
        self, workspace_id: WorkspaceId, repository: str, base_ref: str
    ) -> Workspace:
        repository_root = await _repository_root(repository)
        root_path = self._path_for(workspace_id)
        if root_path.is_dir():
            return Workspace(
                workspace_id=workspace_id,
                root_path=str(root_path),
                repository=repository_root,
                base_ref=base_ref,
                ref=_branch_for(workspace_id),
            )
        return await self._checkout(workspace_id, repository_root, base_ref)

    async def detach(self, workspace_id: WorkspaceId) -> None:
        root_path = self._path_for(workspace_id)
        if not root_path.is_dir():
            return
        await _snapshot(root_path)
        await _git(str(root_path), "worktree", "remove", "--force", str(root_path))

    async def dispose(self, workspace_id: WorkspaceId) -> None:
        """Remove the checkout and delete the branch holding its work.

        A workspace that is already detached keeps its branch: there is no
        checkout left to run git in, and guessing at a repository to delete a
        branch from is worse than leaving one behind.
        """
        root_path = self._path_for(workspace_id)
        branch = _branch_for(workspace_id)
        repository_root = (
            await _main_worktree(root_path) if root_path.is_dir() else None
        )
        if root_path.is_dir():
            await _git(str(root_path), "worktree", "remove", "--force", str(root_path))
        if repository_root is None:
            # Nothing left to run git in: the checkout is already gone, and the
            # branch it left behind is somebody else's to sweep up.
            return
        if await _branch_exists(repository_root, branch):
            await _git(repository_root, "branch", "--delete", "--force", branch)

    async def _checkout(
        self,
        workspace_id: WorkspaceId,
        repository_root: str,
        base_ref: str,
        *,
        resolved_base: str | None = None,
    ) -> Workspace:
        """Put a worktree at this workspace's path, on this workspace's branch."""
        root_path = self._path_for(workspace_id)
        branch = _branch_for(workspace_id)
        self._root_directory.mkdir(parents=True, exist_ok=True)
        # A directory removed behind git's back -- a swept /tmp, an `rm -rf` --
        # leaves an administrative entry that would refuse the checkout below.
        await _git(repository_root, "worktree", "prune")
        if await _branch_exists(repository_root, branch):
            holder = await _checkout_holding(repository_root, branch)
            if holder is not None:
                raise BranchInUseError(branch, holder)
            await _git(repository_root, "worktree", "add", str(root_path), branch)
        else:
            await _git(
                repository_root,
                "worktree",
                "add",
                "-b",
                branch,
                str(root_path),
                resolved_base or base_ref,
            )
        return Workspace(
            workspace_id=workspace_id,
            root_path=str(root_path),
            repository=repository_root,
            base_ref=base_ref,
            ref=branch,
        )

    def _path_for(self, workspace_id: WorkspaceId) -> Path:
        name = str(workspace_id)
        if not name or Path(name).name != name:
            raise ValueError(f"invalid workspace id {workspace_id!r}")
        return self._root_directory / name


class GitWorktreeError(RuntimeError):
    """Git could not create or manage a workspace."""


class BranchInUseError(GitWorktreeError):
    """Somewhere else already has this workspace's branch checked out.

    Git allows one checkout per branch, and the other one is usually a person
    reading what the agent did -- which is what keeping the work on a branch is
    for. So this says where the branch went and how to give it back, rather
    than forcing a second checkout of it.
    """

    def __init__(self, ref: str, checkout: str) -> None:
        super().__init__(
            f"{ref} is already checked out at {checkout}\n"
            f"hint: switch that checkout to another branch first via "
            f"`git -C {checkout} switch -`"
        )
        self.ref = ref
        self.checkout = checkout


def _branch_for(workspace_id: WorkspaceId) -> str:
    return f"engine/{workspace_id}"


async def _checkout_holding(repository_root: str, branch: str) -> str | None:
    """Which worktree, if any, is sitting on this branch."""
    listing = await _git(repository_root, "worktree", "list", "--porcelain")
    path: str | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ").strip()
        elif line.strip() == f"branch refs/heads/{branch}":
            return path
    return None


async def _repository_root(repository: str) -> str:
    return await _git(repository, "rev-parse", "--show-toplevel")


async def _resolve_base(repository_root: str, base_ref: str) -> str:
    """Resolve the workflow base without moving the developer's local branch."""
    if not base_ref.startswith("origin/"):
        return base_ref

    branch = base_ref.removeprefix("origin/")
    temporary_ref = f"refs/engine/provisioning/{uuid4().hex}"
    try:
        try:
            await _git(
                repository_root,
                "fetch",
                "--no-tags",
                "origin",
                f"+refs/heads/{branch}:{temporary_ref}",
            )
        except GitWorktreeError as error:
            if "couldn't find remote ref" not in str(error):
                raise
            raise GitWorktreeError(
                f"Configured default branch {branch!r} does not exist on remote "
                f"'origin'. Create that branch, or set default_branch in engine.toml "
                "to a branch that exists."
            ) from error
        return await _git(
            repository_root, "rev-parse", "--verify", f"{temporary_ref}^{{commit}}"
        )
    finally:
        await _git(repository_root, "update-ref", "-d", temporary_ref)


async def _main_worktree(root_path: Path) -> str:
    """The repository a worktree belongs to -- where its branch can be managed."""
    listing = await _git(str(root_path), "worktree", "list", "--porcelain")
    first = listing.splitlines()[0]
    return first.removeprefix("worktree ").strip()


async def _branch_exists(repository_root: str, branch: str) -> bool:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        repository_root,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
    )
    return await process.wait() == 0


async def _snapshot(root_path: Path) -> None:
    """Commit whatever the agent left lying around, so detaching cannot lose it.

    Uncommitted work is the normal state of an agent's worktree, and removing
    the worktree would take it with it. A commit on the workspace's own branch
    keeps it reviewable by exactly the means everything else here uses.
    """
    if not await _git(str(root_path), "status", "--porcelain"):
        return
    await _git(str(root_path), "add", "--all")
    # `-c` outranks the config file, so the fallback identity is only passed
    # when the repository has none of its own to be outranked.
    identity: tuple[str, ...] = ()
    if not await _configured(root_path, "user.email"):
        name, email = FALLBACK_IDENTITY
        identity = ("-c", f"user.name={name}", "-c", f"user.email={email}")
    await _git(
        str(root_path),
        *identity,
        "commit",
        # A snapshot is bookkeeping; the repository's hooks did not ask for it.
        "--no-verify",
        "--message",
        SNAPSHOT_MESSAGE,
    )


async def _configured(root_path: Path, setting: str) -> bool:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(root_path),
        "config",
        "--get",
        setting,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    return process.returncode == 0 and bool(stdout.strip())


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


__all__ = ["BranchInUseError", "GitWorktreeError", "GitWorktreeWorkspaceProvider"]

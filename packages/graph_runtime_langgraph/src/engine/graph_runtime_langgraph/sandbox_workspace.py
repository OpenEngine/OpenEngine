"""Host-owned snapshots and patches for disposable ACP workspaces.

Only files cross back from a guest. Its index, configuration, hooks and objects
are never used to compute or apply a patch on the host.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from typing import Literal
from uuid import uuid4

from engine.ports import GitResult, Sandbox, SandboxInstance, SandboxSpec
from engine.ports.sandbox import SandboxCredentialRefresh, command_environment
from engine.ports.sandbox_tasks import filesystem_work as _work
from .sandbox_lifecycle import AgentSandbox, guest_command
from engine.ports.sandbox_home import SandboxHome

WorkspacePolicy = Literal["apply", "read-only"]
MAX_PATCH_BYTES = 32 * 1024 * 1024


_READ_GIT_FLAGS = {
    "status": {"--short", "--porcelain", "--branch", "-s", "-b", "-sb"},
    "diff": {"--stat", "--name-only", "--name-status", "--numstat", "--shortstat", "--check", "--cached", "--staged", "--binary", "--patch", "-p", "--no-renames", "--no-ext-diff", "--no-textconv"},
    "log": {"--oneline", "--stat", "--name-only", "--name-status", "--patch", "-p", "--no-patch", "--all", "--graph", "--reverse"},
    "show": {"--oneline", "--stat", "--name-only", "--name-status", "--patch", "-p", "--no-patch", "--no-ext-diff", "--no-textconv"},
    "rev-parse": {"--verify", "--short", "--abbrev-ref", "--show-toplevel", "--is-inside-work-tree"},
    "ls-files": {"--cached", "--others", "--exclude-standard", "--stage", "-s", "-z", "--modified", "--deleted"},
    "ls-tree": {"-r", "-t", "-z", "--name-only", "--long"},
    "branch": {"--show-current", "--list", "--all", "-a"},
}
_READ_GIT_VALUES = ("--format=", "--pretty=", "--max-count=", "--unified=", "--color=", "--porcelain=")


def _inspection_git(arguments: Sequence[str]) -> tuple[str, ...]:
    args = tuple(arguments)
    if not args or args[0] not in _READ_GIT_FLAGS:
        message = "read-only nodes permit only safe Git inspection commands"
        raise ValueError(message)
    command = args[0]
    paths = False
    for arg in args[1:]:
        if arg == "--":
            paths = True
        elif not paths and arg.startswith("-") and arg not in _READ_GIT_FLAGS[command] and not arg.startswith(_READ_GIT_VALUES) and not re.fullmatch(r"-\d+", arg):
            message = "Git option is not permitted for read-only inspection"
            raise ValueError(message)
    if command == "branch" and len(args) > 1 and not any(arg in {"--show-current", "--list", "--all", "-a"} for arg in args[1:]):
        message = "read-only nodes cannot create branches"
        raise ValueError(message)
    if command == "branch":
        if "--show-current" in args[1:]:
            if args != ("branch", "--show-current"):
                message = "read-only branch inspection cannot combine --show-current with other arguments"
                raise ValueError(message)
        else:
            args = ("branch", "--list", *args[1:])
    # Disable attributes invoking host-side programs, including when showing
    # patches through log/show. All config, hooks and objects are host-owned.
    if command in {"diff", "log", "show"}:
        args = (command, "--no-ext-diff", "--no-textconv", *args[1:])
    return args


class WorkspaceCredentialLeak(RuntimeError):
    """Credential-bearing files cannot become an input copy or output patch."""


def _credential_in(path: Path, relative: Path, secrets: tuple[str, ...]) -> bool:
    needles = [value.encode() for value in secrets if value]
    if not needles:
        return False
    if any(value in os.fsencode(relative) for value in needles):
        return True
    if path.is_symlink():
        return any(value in os.fsencode(os.readlink(path)) for value in needles)
    if not path.is_file():
        return False
    overlap = max(map(len, needles))
    tail = b""
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            data = tail + chunk
            if any(value in data for value in needles):
                return True
            tail = data[-overlap:]
    return False


class WorkspacePatchConflict(RuntimeError):
    """A rejected patch retained with the node's failure, not in a temporary VM."""

    def __init__(self, message: str, patch: bytes) -> None:
        super().__init__(message)
        self.patch = patch

    def attachment(self) -> dict[str, str]:
        return {
            "name": "workspace.patch", "mediaType": "text/x-diff",
            "encoding": "base64", "content": base64.b64encode(self.patch).decode("ascii"),
        }


def _git(directory: Path, *args: str, data: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    environment = {
        **command_environment(), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
    }
    result = subprocess.run(
        ["git", "--literal-pathspecs", "-c", f"core.hooksPath={os.devnull}",
         "-c", "core.autocrlf=false", "-C", str(directory), *args],
        input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment, timeout=120, check=False,
    )
    if check and result.returncode:
        message = "workspace Git operation failed: " + result.stderr.decode(errors="replace")[:2000]
        raise RuntimeError(message)
    return result


def _metadata(path: Path) -> bool:
    # Git on case-insensitive hosts must not interpret exported files as metadata.
    return any(part.casefold() == ".git" for part in path.parts)


def _files(source: Path) -> list[Path]:
    root = _git(source, "rev-parse", "--show-toplevel", check=False)
    if root.returncode == 0:
        if Path(os.fsdecode(root.stdout).strip()).resolve() != source:
            message = "sandbox workspace must be a repository root, not a subdirectory"
            raise ValueError(message)
        names = _git(source, "ls-files", "--cached", "--others", "--exclude-standard", "-z").stdout
        files = [Path(os.fsdecode(name)) for name in names.split(b"\0") if name and not _metadata(Path(os.fsdecode(name)))]
        if any((source / path).is_dir() and not (source / path).is_symlink() for path in files):
            message = "workspace snapshots do not support Git submodules"
            raise ValueError(message)
        return files
    return [path.relative_to(source) for path in source.rglob("*") if not _metadata(path.relative_to(source))]


def _safe_path(root: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts or _metadata(relative):
        message = "workspace file must stay outside Git metadata and inside its snapshot"
        raise ValueError(message)
    target = root / relative
    for ancestor in target.parents:
        if ancestor == root:
            break
        if ancestor.is_symlink():
            message = "workspace file has a symlink ancestor"
            raise ValueError(message)
    return target


def _identity(path: Path) -> tuple[object, ...]:
    try:
        mode = path.lstat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return (None,)
    if stat.S_ISLNK(mode):
        return ("link", os.readlink(path))
    if stat.S_ISREG(mode):
        return ("file", bool(mode & stat.S_IXUSR), path.read_bytes())
    if stat.S_ISDIR(mode):
        return ("directory",)
    message = "workspace contains a non-file entry"
    raise ValueError(message)


def _snapshot(source: Path, destination: Path, secrets: tuple[str, ...] = ()) -> tuple[str, str]:
    files = _files(source)
    head = _git(source, "rev-parse", "--verify", "HEAD", check=False)
    branch = _git(source, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
    destination.mkdir()
    if head.returncode == 0:
        # A real standalone repository, never a worktree's pointer into the host.
        # Mirror refs as well as objects so reviewers retain the run's base refs.
        _git(source, "clone", "--quiet", "--mirror", "--no-local", "--no-hardlinks",
             "--template=", "--", str(source), str(destination / ".git"))
        _git(destination, "config", "core.bare", "false")
        _git(destination, "config", "--remove-section", "remote.origin")
    else:
        _git(destination, "init", "--quiet", "--template=")
        if branch:
            _git(destination, "symbolic-ref", "HEAD", branch)
    for relative in files:
        original = _safe_path(source, relative)
        target = _safe_path(destination, relative)
        if _credential_in(original, relative, secrets):
            message = "model credentials must not be present in the repository copy"
            raise WorkspaceCredentialLeak(message)
        if original.is_symlink():
            if not original.resolve().is_relative_to(source):
                message = "workspace symlink leaves its snapshot"
                raise ValueError(message)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(original))
        elif original.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
        elif original.exists() and not original.is_dir():
            message = "workspace contains a non-file entry"
            raise ValueError(message)
    _git(destination, "add", "--all", "--force")
    return _git(destination, "write-tree").stdout.decode().strip(), branch


# Export to an unpredictable staging directory in the guest. No guest Git
# command runs on the host. copy_out performs its normal bounded extraction and
# link checks before the host's independent Git index ever sees these files.
_EXPORT = '''import os, pathlib, shutil, sys
root = pathlib.Path.cwd()
target = root / sys.argv[1]
def ignored(directory, names):
    return [n for n in names if n.casefold() == '.git' or (pathlib.Path(directory) == root and n == target.name)]
shutil.copytree(root, target, symlinks=True, ignore=ignored)
'''
_REFRESH = """import json, pathlib, shutil, sys
root = pathlib.Path.cwd()
for name in [*json.loads(sys.argv[1]), '.git']:
    relative = pathlib.Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('invalid refresh path')
    target = root / relative
    if any(p.is_symlink() for p in target.parents if p != root and root in p.parents):
        raise ValueError('refresh path has a symlink ancestor')
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
"""
_READ_ONLY = '''import os, pathlib, stat
root = pathlib.Path.cwd()
for directory, directories, files in os.walk(root, followlinks=False):
    for name in files + directories:
        path = pathlib.Path(directory) / name
        if not path.is_symlink():
            path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
root.chmod(stat.S_IMODE(root.stat().st_mode) & ~0o222)
'''


@dataclass
class WorkspaceCopy:
    source: Path
    root: Path
    lock: asyncio.Lock
    policy: WorkspacePolicy
    tree: str = ""
    branch: str = ""
    exported: bool = False
    redactions: tuple[str, ...] = field(default=(), repr=False)
    active: SandboxInstance | None = field(default=None, repr=False)
    failure: Exception | None = field(default=None, repr=False)
    home: SandboxHome | None = field(default=None, repr=False)
    agent: AgentSandbox | None = field(default=None, repr=False)

    @property
    def snapshot(self) -> Path:
        return self.root / "snapshot"

    @property
    def output(self) -> Path:
        return self.root / "output"

    def backend(self, backend: Sandbox) -> Sandbox:
        copy = self

        class CopyBackend:
            @asynccontextmanager
            async def create(self, spec: SandboxSpec) -> AsyncIterator[SandboxInstance]:
                copy.home = spec.home
                copy.redactions = spec.home.all_redactions if spec.home is not None else ()
                async with copy.lock:
                    copy.tree, copy.branch = await _work(_snapshot, copy.source, copy.snapshot, copy.redactions)
                async with backend.create(replace(spec, workspace=copy.snapshot)) as sandbox:
                    copy.active = sandbox
                    agent = AgentSandbox(sandbox)
                    copy.agent = agent
                    if copy.policy == "read-only":
                        await guest_command(sandbox, _READ_ONLY)
                    try:
                        yield agent
                    finally:
                        await agent.stop()
                        if copy.policy == "apply":
                            await copy.export()
                        copy.active = None

        return CopyBackend()

    async def inspect_git(self, arguments: Sequence[str]) -> GitResult:
        args = _inspection_git(arguments)
        result = await _work(lambda: _git(self.snapshot, *args, check=False))
        return GitResult(exit_code=result.returncode,
            stdout=result.stdout.decode(errors="replace"), stderr=result.stderr.decode(errors="replace"))

    async def export(self) -> None:
        if self.agent is not None:
            async with self.agent.paused():
                await self._export_stopped()
        else:
            await self._export_stopped()

    async def _export_stopped(self) -> None:
        if self.active is None:
            message = "node workspace is not active"
            raise RuntimeError(message)
        if self.home is not None and self.home.credentials is not None:
            if not isinstance(self.active, SandboxCredentialRefresh):
                message = "sandbox cannot refresh credential export filters"
                raise RuntimeError(message)
            await self.active.refresh_credentials()
            self.redactions = self.home.all_redactions
        await _work(shutil.rmtree, self.output, True)
        name = ".oe-export-" + uuid4().hex
        try:
            await guest_command(self.active, _EXPORT, name)
            await self.active.copy_out(name, self.output)
            self.exported = True
        finally:
            await guest_command(self.active, "import shutil,sys; shutil.rmtree(sys.argv[1], ignore_errors=True)", name)

    def _resnapshot(self) -> None:
        shutil.rmtree(self.snapshot)
        self.tree, self.branch = _snapshot(self.source, self.snapshot, self.redactions)
        self.exported = False

    @asynccontextmanager
    async def host_operation(self) -> AsyncIterator[None]:
        """Checkpoint edits at an approved host tool boundary, then refresh Git.

        Never imports guest Git metadata. A checkpoint is an explicit copy-back;
        later failure cannot undo an already approved commit/push/PR.
        """
        if self.policy == "read-only":
            message = "host repository mutations are disabled for read-only nodes"
            raise RuntimeError(message)
        if self.active is None:
            message = "node workspace is not active"
            raise RuntimeError(message)
        async with self.lock:
            if self.failure is not None:
                raise self.failure
            try:
                await self.export()
                patch, paths = await _work(self._patch)
                # These host-owned names preserve ignored guest build caches on
                # refresh, while removing files a host checkout/reset deleted.
                known = (await _work(_git, self.root, f"--git-dir={self.snapshot / '.git'}", f"--work-tree={self.output}",
                    "ls-files", "--cached", "-z")).stdout
                if patch:
                    await _work(self._apply, patch, paths)
                else:
                    await _work(self._check_branch, patch)
            except Exception as error:
                self.failure = error
                raise
            try:
                yield
            finally:
                try:
                    await _work(self._resnapshot)
                    refreshed = (await _work(_git, self.snapshot, "ls-files", "--cached", "-z")).stdout
                    await guest_command(self.active, _REFRESH, json.dumps([os.fsdecode(p) for p in (known + refreshed).split(b"\0") if p]))
                    await self.active.copy_in(self.snapshot, ".")
                except Exception as error:
                    self.failure = error
                    raise

    def _patch(self) -> tuple[bytes, list[Path]]:
        if self.home is not None:
            self.redactions = self.home.all_redactions
        # Use the host-owned index and baseline. .git in the guest was excluded
        # on export and is never consulted, even if the agent committed/reset it.
        for path in self.output.rglob("*"):
            if _credential_in(path, path.relative_to(self.output), self.redactions):
                message = "node output contains model credentials; patch was not created or applied"
                raise WorkspaceCredentialLeak(message)
            resolved = path.resolve()
            bad_link = path.is_symlink() and (
                not resolved.is_relative_to(self.output)
                or _metadata(resolved.relative_to(self.output))
            )
            if _metadata(path.relative_to(self.output)) or bad_link:
                message = "exported workspace contains Git metadata or an escaping symlink"
                raise ValueError(message)
        gitdir = str(self.snapshot / ".git")
        args = (f"--git-dir={gitdir}", f"--work-tree={self.output}")
        _git(self.root, *args, "add", "--all")
        patch = _git(self.root, *args, "diff", "--cached", "--binary", "--full-index",
                     "--no-ext-diff", "--no-textconv", "--no-renames", self.tree, "--").stdout
        if len(patch) > MAX_PATCH_BYTES:
            message = "workspace patch exceeds byte budget"
            raise ValueError(message)
        names = _git(self.root, *args, "diff", "--cached", "--name-only", "-z", self.tree, "--").stdout
        return patch, [Path(os.fsdecode(name)) for name in names.split(b"\0") if name]

    def _apply(self, patch: bytes, paths: list[Path]) -> None:
        try:
            self._apply_checked(patch, paths)
        except WorkspacePatchConflict:
            raise
        except (OSError, ValueError, RuntimeError) as error:
            message = f"workspace patch could not be applied: {error}"
            raise WorkspacePatchConflict(message, patch) from error

    def _check_branch(self, patch: bytes) -> None:
        branch = _git(self.source, "symbolic-ref", "-q", "HEAD", check=False).stdout.decode().strip()
        if branch != self.branch:
            message = "workspace branch changed while the node was running; patch was not applied"
            raise WorkspacePatchConflict(message, patch)

    def _apply_checked(self, patch: bytes, paths: list[Path]) -> None:
        self._check_branch(patch)
        for relative in paths:
            current = _safe_path(self.source, relative)
            original = _safe_path(self.snapshot, relative)
            if _identity(current) != _identity(original):
                message = f"workspace patch conflicts with host changes to {relative}; patch was not applied"
                raise WorkspacePatchConflict(message, patch)
        result = _git(self.source, "apply", "--binary", "--whitespace=nowarn", "--check", "-", data=patch, check=False)
        if result.returncode == 0:
            result = _git(self.source, "apply", "--binary", "--whitespace=nowarn", "-", data=patch, check=False)
        if result.returncode:
            message = "workspace patch could not be applied: " + result.stderr.decode(errors="replace")[:2000]
            raise WorkspacePatchConflict(message, patch)

    async def accept(self) -> bytes:
        if self.failure is not None:
            raise self.failure
        if self.policy == "read-only":
            return b""
        if not self.exported:
            message = "sandbox closed without exporting its workspace"
            raise RuntimeError(message)
        patch, paths = await _work(self._patch)
        if patch:
            async with self.lock:
                await _work(self._apply, patch, paths)
        return patch


class WorkspaceCopies:
    """Serialize snapshots and patch application, never the agent executions."""

    def __init__(self) -> None:
        self._locks: dict[Path, asyncio.Lock] = {}

    @asynccontextmanager
    async def create(self, source: str, policy: WorkspacePolicy) -> AsyncIterator[WorkspaceCopy]:
        if policy not in {"apply", "read-only"}:
            message = "workspace policy must be apply or read-only"
            raise ValueError(message)
        path = Path(source).resolve()
        if not path.is_dir():
            message = "node workspace directory does not exist"
            raise FileNotFoundError(message)
        lock = self._locks.setdefault(path, asyncio.Lock())
        with tempfile.TemporaryDirectory(prefix="oe-node-workspace-") as temporary:
            yield WorkspaceCopy(path, Path(temporary).resolve(), lock, policy)

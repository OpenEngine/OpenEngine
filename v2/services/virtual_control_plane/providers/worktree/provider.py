"""Git worktree / host-process adapter behind the managed `AllocationProvider` port.

Each runtime owns one directory beneath the configured root, named by its
runtime ID:

    {root}/{runtime_id}/owner.json   ownership and the resolved repositories, written first
    {root}/{runtime_id}/workspace/   the workspace root an agent sees
        repositories/{index}/        a detached Git worktree per requested repository
    {root}/{runtime_id}/state/       the daemon's private journal (0700), outside the workspace
    {root}/{runtime_id}/ready        preparation marker naming the runtime

The runtime directory appears atomically with `owner.json` inside it (renamed
from a pending directory), so a directory without a matching record is never
ours and is never adopted, prepared or removed. The record holds each
repository's source, the commit its `ref` resolved to and the deterministic
worktree path, so a resumed provision repeats the same worktree at the same
revision. Worktrees are created detached with `git worktree add --lock`, whose
lock reason names the workspace and runtime: VCP never creates, moves or
deletes a branch, and only touches a Git worktree registered at its own path.

Isolation is a host process: a worktree separates files, not credentials,
network or process access. Capabilities say `process` and nothing more.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import stat
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn
from urllib.parse import unquote, urlsplit

from openengine.messaging import ErrorCode, ServiceError
from openengine.messaging.errors import invalid_argument

from virtual_control_plane.application.ports import Ownership, OwnershipMismatch, RuntimeContext
from virtual_control_plane.persistence.store import Locator
from virtual_control_plane.providers.worktree.daemon import HostDaemon

PROVIDER = "process"
DEFAULT_TOOLS = ("git", "terminal")
HOST_TOOLS = {"terminal": "sh"}
OWNER_FILE = "owner.json"
READY_FILE = "ready"
WORKSPACE = "workspace"
STATE = "state"
INSTRUCTIONS_FILE = "AGENTS.md"
COMMAND_TIMEOUT_SECONDS = 120
TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _unavailable(message: str, **details: Any) -> NoReturn:
    """Transient: the lifecycle retries the same ownership with capped backoff."""
    raise ServiceError(ErrorCode.UNAVAILABLE, message, retryable=True, details=details or None)


def _terminal(message: str, **details: Any) -> NoReturn:
    """Final for this operation; ownership is retained for explicit cleanup."""
    raise ServiceError(ErrorCode.PROVIDER_ERROR, message, retryable=False, details=details or None)


def lock_reason(ownership: Ownership) -> str:
    """Recorded in Git's worktree metadata; the same across generations, like the runtime directory."""
    return f"openengine-vcp {ownership.workspace_id} {ownership.runtime_id}"


def _relative(path: str) -> PurePosixPath:
    relative = PurePosixPath(path)
    if not path or "\0" in path or relative.is_absolute() or any(part in ("..", ".", "") for part in path.split("/")):
        raise ValueError(path)
    return relative


def _source(url: str) -> str:
    """A local repository path from an absolute path or `file://` URL; remote URLs are not worktrees."""
    if url.startswith("file://"):
        parts = urlsplit(url)
        if parts.netloc not in ("", "localhost") or parts.query or parts.fragment:
            raise ValueError(url)
        url = unquote(parts.path)
    if "\0" in url or not os.path.isabs(url):
        raise ValueError(url)
    return url


def _directory(path: Path) -> os.stat_result | None:
    """The path's own status without following a final symlink, or None if absent."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _make_directory(path: Path, mode: int = 0o755) -> None:
    """Create one level, refusing anything already there that is not a real directory."""
    try:
        os.mkdir(path, mode)
    except FileExistsError:
        pass
    status = os.lstat(path)
    if not stat.S_ISDIR(status.st_mode):
        _terminal("an owned workspace path is not a directory", path=str(path))


def _write_beneath(root: Path, relative: str, content: bytes) -> None:
    """Write a file beneath `root` without following any symlink on the way, or fail terminally."""
    parts = _relative(relative).parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, 0o755, dir_fd=descriptor)
            except FileExistsError:
                pass
            following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        output = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644, dir_fd=descriptor)
        with os.fdopen(output, "wb") as file:
            file.write(content)
    except OSError as error:
        _terminal("context file path does not stay beneath the workspace root", path=relative,
                  reason=error.strerror or str(error))
    finally:
        os.close(descriptor)


def _write_atomically(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def _sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove(path: Path) -> None:
    """Remove a path inside an owned runtime directory without following symlinks."""
    status = _directory(path)
    if status is None:
        return
    if stat.S_ISDIR(status.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


class Worktree:
    """One adapter per owned root. `daemon` supervises each runtime's workspace daemon.

    `discard_changes` is the operator's explicit cleanup authorization: without
    it, destroy refuses a worktree with uncommitted changes or commits no ref
    contains, and keeps everything for the operator to resolve.
    """

    def __init__(self, root: Path | str, *, tools: Sequence[str] = DEFAULT_TOOLS, daemon: HostDaemon | None = None,
                 discard_changes: bool = False, git: str = "git",
                 command_timeout: float = COMMAND_TIMEOUT_SECONDS) -> None:
        root = Path(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = root.resolve(strict=True)
        self.tools = tuple(dict.fromkeys(tools))
        self.daemon, self.discard_changes = daemon, discard_changes
        self.git, self.command_timeout = git, command_timeout

    @property
    def capabilities(self) -> dict[str, Any]:
        # Honest: a host process sharing the operator's credentials, network
        # and process namespace; no resource limits, checkpoint or suspend.
        return {"provider": PROVIDER, "isolation": "process", "checkpoint": False, "suspend": False,
                "restore": "none", "approvals_survive_restore": False, "max_resources": {},
                "tools": list(self.tools)}

    # -- port -----------------------------------------------------------------

    def validate(self, specification: dict[str, Any]) -> None:
        spec = specification["spec"]
        violations = []
        if spec.get("resources"):
            violations.append(("payload.workspace.spec.resources", "process workspaces cannot enforce resource limits"))
        context = spec.get("context", {})
        for index, repository in enumerate(context.get("repositories", [])):
            try:
                _source(repository["url"])
            except ValueError:
                violations.append((f"payload.workspace.spec.context.repositories[{index}].url",
                                   "must be a local repository path or file:// URL"))
            if repository.get("ref", "").startswith("-"):
                violations.append((f"payload.workspace.spec.context.repositories[{index}].ref", "must not start with '-'"))
        for index, file in enumerate(context.get("files", [])):
            try:
                _relative(file["path"])
            except ValueError:
                violations.append((f"payload.workspace.spec.context.files[{index}].path",
                                   "must be a relative path staying beneath the workspace root"))
        if violations:
            raise invalid_argument(violations)

    async def provision(self, ownership: Ownership, specification: dict[str, Any]) -> Locator:
        directory = self._directory(ownership)
        record = self._owned(ownership)
        if record is None:
            record = await self._claim(ownership, specification["spec"])
        if not self._prepared(ownership):
            await self._prepare(ownership, record, specification["spec"])
        await self._start(ownership, initialize=True)
        return self._locator(directory, record)

    async def ensure(self, ownership: Ownership, locator: Locator, specification: dict[str, Any]) -> Locator:
        directory = self._directory(ownership)
        self._check_locator(directory, locator)
        record = self._owned(ownership)
        if record is None or not self._prepared(ownership):
            # Never reprovision silently: the workspace's files would be gone.
            _terminal("owned worktree workspace no longer exists", runtime_id=ownership.runtime_id)
        for repository in record["repositories"]:
            await self._verify(ownership, repository)
        await self._start(ownership, initialize=False)
        return self._locator(directory, record)

    async def destroy(self, ownership: Ownership, locator: Locator | None) -> None:
        directory = self._directory(ownership)
        self._check_locator(directory, locator)
        _remove(self._pending(ownership))  # a claim that never became visible
        record = self._owned(ownership, destroying=True)
        if record is None:
            return
        prepared = self._prepared(ownership)
        # Every refusal is decided before anything stops or changes.
        for repository in record["repositories"]:
            await self._release(ownership, repository, prepared=prepared, check_only=True)
        await self._unrelated(directory, record)
        if self.daemon is not None:
            await self.daemon.stop(directory, directory / STATE)
        elif HostDaemon.running(directory / STATE):
            _unavailable("a workspace daemon still owns this runtime's journal")
        for repository in record["repositories"]:
            await self._release(ownership, repository, prepared=prepared, check_only=False)
        for name in sorted(os.listdir(directory)):
            if name != OWNER_FILE:
                _remove(directory / name)
        # The record goes last: an interrupted destroy still finds it and resumes.
        (directory / OWNER_FILE).unlink(missing_ok=True)
        directory.rmdir()
        _sync(self.root)
        if _directory(directory) is not None:
            _unavailable("owned runtime directory still exists after removal", runtime_id=ownership.runtime_id)

    def context(self, ownership: Ownership, locator: Locator, specification: dict[str, Any]) -> RuntimeContext:
        context = specification["spec"].get("context", {})
        return RuntimeContext(
            root=str(self._directory(ownership) / WORKSPACE),
            working_directory="repositories/0" if context.get("repositories") else ".",
            instruction_files=(INSTRUCTIONS_FILE,) if context.get("instructions") else (),
        )

    # -- ownership -------------------------------------------------------------

    def _directory(self, ownership: Ownership) -> Path:
        if not TOKEN.match(ownership.runtime_id) or not TOKEN.match(ownership.workspace_id):
            _terminal("runtime identity is not a safe directory name")
        return self.root / ownership.runtime_id

    def _pending(self, ownership: Ownership) -> Path:
        return self.root / f".{ownership.runtime_id}.pending"

    def _locator(self, directory: Path, record: dict[str, Any]) -> Locator:
        return {"directory": str(directory), "root": str(directory / WORKSPACE),
                "repositories": [dict(repository) for repository in record["repositories"]]}

    @staticmethod
    def _check_locator(directory: Path, locator: Locator | None) -> None:
        if locator and locator.get("directory") not in (None, str(directory)):
            _terminal("locator does not name the owned runtime directory", directory=str(directory))

    def _owned(self, ownership: Ownership, *, destroying: bool = False) -> dict[str, Any] | None:
        """The verified ownership record, None if the runtime directory is absent, else `OwnershipMismatch`.

        Provision and ensure accept any generation of the same runtime; destroy
        only removes the exact generation it owns.
        """
        directory = self._directory(ownership)
        status = _directory(directory)
        if status is None:
            return None
        if not stat.S_ISDIR(status.st_mode):
            raise OwnershipMismatch(PROVIDER, str(directory))
        try:
            descriptor = os.open(directory / OWNER_FILE, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as file:
                record = json.load(file)
            repositories = record["repositories"]
            matches = (record["provider"] == PROVIDER and record["workspace_id"] == ownership.workspace_id
                       and record["runtime_id"] == ownership.runtime_id
                       and (not destroying or record["generation"] == ownership.generation))
            # A record may only name the deterministic worktree paths inside its own directory.
            matches = matches and all(
                repository["path"] == str(directory / WORKSPACE / "repositories" / str(index))
                and isinstance(repository["source"], str) and isinstance(repository["revision"], str)
                for index, repository in enumerate(repositories))
        except (OSError, ValueError, KeyError, TypeError):
            matches = False
        if not matches:
            raise OwnershipMismatch(PROVIDER, str(directory))
        return record

    async def _claim(self, ownership: Ownership, spec: dict[str, Any]) -> dict[str, Any]:
        """Resolve each repository once, then make the directory and its record appear together."""
        directory = self._directory(ownership)
        repositories = []
        for index, repository in enumerate(spec.get("context", {}).get("repositories", [])):
            source, revision = await self._resolve(repository)
            repositories.append({"source": source, "revision": revision,
                                 "path": str(directory / WORKSPACE / "repositories" / str(index))})
        record = {"provider": PROVIDER, "workspace_id": ownership.workspace_id, "runtime_id": ownership.runtime_id,
                  "generation": ownership.generation, "repositories": repositories}
        pending = self._pending(ownership)
        _remove(pending)
        os.mkdir(pending, 0o700)
        _write_atomically(pending / OWNER_FILE, json.dumps(record, sort_keys=True))
        _sync(pending)
        os.rename(pending, directory)
        _sync(self.root)
        return record

    def _prepared(self, ownership: Ownership) -> bool:
        try:
            descriptor = os.open(self._directory(ownership) / READY_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return False
        with os.fdopen(descriptor) as file:
            return file.read().strip() == ownership.runtime_id

    # -- preparation -----------------------------------------------------------

    async def _prepare(self, ownership: Ownership, record: dict[str, Any], spec: dict[str, Any]) -> None:
        """Idempotent steps; nothing ran in this workspace before the ready marker, so each may be redone."""
        directory = self._directory(ownership)
        context = spec.get("context", {})
        required = [HOST_TOOLS.get(tool, tool) for tool in spec.get("tools", [])]
        if record["repositories"]:
            required.append(self.git)
        for tool in dict.fromkeys(required):
            if shutil.which(tool) is None:
                _terminal(f"host lacks required tool {tool!r}", tool=tool)
        root = directory / WORKSPACE
        _make_directory(root)
        _make_directory(directory / STATE, 0o700)
        if record["repositories"]:
            _make_directory(root / "repositories")
        for repository in record["repositories"]:
            await self._materialize(ownership, repository)
        for file in context.get("files", []):
            _write_beneath(root, file["path"], file["content"].encode())
        if instructions := context.get("instructions"):
            _write_beneath(root, INSTRUCTIONS_FILE, instructions.encode())
        _write_atomically(directory / READY_FILE, ownership.runtime_id)
        _sync(directory)

    async def _start(self, ownership: Ownership, *, initialize: bool) -> None:
        if self.daemon is None:
            return
        directory = self._directory(ownership)
        await self.daemon.start(ownership, directory, directory / WORKSPACE, directory / STATE, initialize=initialize)

    # -- Git -------------------------------------------------------------------

    async def _resolve(self, repository: dict[str, Any]) -> tuple[str, str]:
        """The source repository's real path and the commit `ref` (default: its HEAD) names now."""
        source = os.path.realpath(_source(repository["url"]))
        if not os.path.isdir(source):
            _terminal("repository is not a local directory", repository=repository["url"])
        if Path(source).is_relative_to(self.root) or self.root.is_relative_to(source):
            _terminal("repository overlaps the owned worktree root", repository=repository["url"])
        output, _, code = await self._run("-C", source, "rev-parse", "--verify", "--quiet", "--end-of-options",
                                          f"{repository.get('ref', 'HEAD')}^{{commit}}", check=False)
        if code:
            _terminal("repository revision does not resolve to a commit", repository=repository["url"],
                      ref=repository.get("ref", "HEAD"))
        return source, output.decode().strip()

    async def _worktrees(self, source: str) -> dict[str, dict[str, str]] | None:
        """Registered worktrees by real path, or None if the source repository is gone."""
        if not os.path.isdir(source):
            return None
        output, _, _ = await self._run("-C", source, "worktree", "list", "--porcelain")
        entries: dict[str, dict[str, str]] = {}
        entry: dict[str, str] = {}
        for line in output.decode(errors="surrogateescape").splitlines() + [""]:
            if not line:
                if "worktree" in entry:
                    entries[os.path.realpath(entry["worktree"])] = entry
                entry = {}
                continue
            key, _, value = line.partition(" ")
            entry[key] = value
        return entries

    @staticmethod
    def _foreign(ownership: Ownership, entry: dict[str, str] | None) -> bool:
        return entry is not None and "locked" in entry and entry["locked"] != lock_reason(ownership)

    async def _belongs(self, path: str, source: str) -> bool:
        """Whether the directory's Git metadata points into the recorded source repository."""
        if not os.path.exists(os.path.join(path, ".git")):
            return False
        mine, _, code = await self._run("-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
        theirs, _, _ = await self._run("-C", source, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return not code and os.path.realpath(mine.decode().strip()) == os.path.realpath(theirs.decode().strip())

    async def _materialize(self, ownership: Ownership, repository: dict[str, Any]) -> None:
        """Create the detached worktree, or finish one an interrupted attempt left behind."""
        path, source, revision = repository["path"], repository["source"], repository["revision"]
        entries = await self._worktrees(source)
        if entries is None:
            _terminal("repository is no longer a local directory", source=source)
        entry = entries.get(path)
        if self._foreign(ownership, entry):
            raise OwnershipMismatch(PROVIDER, path)
        status = _directory(Path(path))
        if status is not None and not stat.S_ISDIR(status.st_mode):
            _terminal("an owned worktree path is not a directory", path=path)
        lock = ["--lock", "--reason", lock_reason(ownership), "--detach", path, revision]
        if entry is None:
            if status is not None:
                # Unregistered and never prepared: a partial directory inside our own runtime.
                shutil.rmtree(path)
            await self._run("-C", source, "worktree", "add", "-q", *lock)
        elif status is None:
            # Registered to our path but missing: Git's supported way to reuse the registration.
            await self._run("-C", source, "worktree", "add", "-q", "--force", "--force", *lock)
        elif await self._belongs(path, source):
            # Ours and not prepared yet, so nothing of an agent's is here to lose.
            await self._run("-C", path, "checkout", "-q", "--force", "--detach", revision)
        else:
            _terminal("worktree metadata does not match the owned path", path=path)
        await self._verify(ownership, repository)

    async def _verify(self, ownership: Ownership, repository: dict[str, Any]) -> None:
        """Both records agree: Git registers this worktree at our path, under our lock, from our source."""
        path, source = repository["path"], repository["source"]
        entries = await self._worktrees(source)
        entry = None if entries is None else entries.get(path)
        status = _directory(Path(path))
        if (entry is None or self._foreign(ownership, entry) or status is None or not stat.S_ISDIR(status.st_mode)
                or not await self._belongs(path, source)):
            _terminal("owned worktree is missing or its Git metadata is stale", path=path)

    async def _release(self, ownership: Ownership, repository: dict[str, Any], *, prepared: bool, check_only: bool) -> None:
        """Unregister and remove one owned worktree, refusing anything ambiguous or unsaved."""
        path, source, revision = repository["path"], repository["source"], repository["revision"]
        entries = await self._worktrees(source)
        entry = None if entries is None else entries.get(path)
        if self._foreign(ownership, entry):
            raise OwnershipMismatch(PROVIDER, path)
        status = _directory(Path(path))
        if status is not None and not stat.S_ISDIR(status.st_mode):
            _terminal("an owned worktree path is not a directory", path=path)
        if entry is not None and status is not None:
            if not await self._belongs(path, source):
                _terminal("worktree metadata does not match the owned path", path=path)
            if not self.discard_changes:
                await self._require_saved(path, source, revision)
            if check_only:
                return
            await self._run("-C", source, "worktree", "unlock", path, check=False)
            await self._run("-C", source, "worktree", "remove", *(["--force"] if self.discard_changes else []), path)
        elif entry is not None:
            # Stale registration for a directory that is already gone.
            if check_only:
                return
            await self._run("-C", source, "worktree", "unlock", path, check=False)
            await self._run("-C", source, "worktree", "remove", "--force", path)
        elif status is not None:
            # A directory Git does not know: partial creation before preparation finished,
            # or metadata someone removed after it. Only the former is safe to delete.
            if prepared and not self.discard_changes:
                _terminal("worktree has no Git metadata; refusing to delete it without the discard policy", path=path)
            if not check_only:
                shutil.rmtree(path)
        if not check_only and _directory(Path(path)) is not None:
            _unavailable("owned worktree still exists after removal", path=path)

    async def _require_saved(self, path: str, source: str, revision: str) -> None:
        changes, _, _ = await self._run("-C", path, "status", "--porcelain", "--untracked-files=all")
        if changes.strip():
            _terminal("worktree has uncommitted changes; commit them or destroy with the discard policy", path=path)
        head, _, _ = await self._run("-C", path, "rev-parse", "HEAD")
        head_commit = head.decode().strip()
        if head_commit == revision:
            return
        # Removing the worktree drops its HEAD; another ref must still hold any new commit.
        refs, _, _ = await self._run("-C", source, "for-each-ref", "--count=1", "--contains", head_commit, "--format=%(refname)")
        if not refs.strip():
            _terminal("worktree has commits no branch or tag contains; refusing to drop them", path=path, head=head_commit)

    async def _unrelated(self, directory: Path, record: dict[str, Any]) -> None:
        """Refuse to delete a worktree someone else registered inside our directory."""
        ours = {repository["path"] for repository in record["repositories"]}
        for source in {repository["source"] for repository in record["repositories"]}:
            for path in await self._worktrees(source) or {}:
                if path not in ours and Path(path).is_relative_to(directory):
                    _terminal("an unrelated Git worktree lives inside the owned workspace", path=path)

    async def _run(self, *arguments: str, check: bool = True) -> tuple[bytes, bytes, int]:
        """One Git command with no inherited repository overrides, killed with its group on timeout."""
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment["GIT_TERMINAL_PROMPT"] = "0"
        try:
            process = await asyncio.create_subprocess_exec(
                self.git, *arguments, env=environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
        except OSError as error:
            _terminal(f"cannot execute {self.git!r}: {error.strerror or error}")
        subcommand = next((argument for argument in arguments[2:] if not argument.startswith("-")), "git")
        try:
            async with asyncio.timeout(self.command_timeout):
                output, errors = await process.communicate()
        except TimeoutError:
            _unavailable(f"git {subcommand} timed out", command=subcommand)
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode and check:
            _unavailable(f"git {subcommand} exited with {process.returncode}", command=subcommand)
        return output, errors, process.returncode or 0

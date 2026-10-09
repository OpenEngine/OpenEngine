"""Host execution with disposable workspace copies, not a security boundary.

Commands inherit the host environment, filesystem access and network access.
Images and egress restrictions are rejected rather than silently ignored.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
import os
from pathlib import Path
import shutil
import signal
import stat
import tempfile

from engine.ports.sandbox import SandboxSpec


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        pass


async def _filesystem_work(function: Callable[..., None], *args: object) -> None:
    # Cancelling to_thread does not stop its worker. Keep ownership until the
    # worker finishes, even if teardown or repeated cancellation arrives.
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
    try:
        worker.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


class ProcessExecution:
    def __init__(self, process: asyncio.subprocess.Process, timeout: float | None):
        self.process = process
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self._expired = False
        self._timer = (
            asyncio.get_running_loop().call_later(timeout, self._expire)
            if timeout is not None else None
        )
        self._waiter = asyncio.create_task(process.wait())

    def _expire(self) -> None:
        self._expired = self.process.returncode is None
        # Descendants can retain the pipes after the command itself exits.
        _kill(self.process)

    async def wait(self) -> int:
        result = await asyncio.shield(self._waiter)
        if self._expired:
            raise TimeoutError("sandbox command timed out")
        return result


class ProcessSandboxInstance:
    def __init__(self, root: Path, spec: SandboxSpec):
        self._root = root
        self._spec = spec
        self._executions: set[ProcessExecution] = set()
        self._destroyed = False
        self._spawn_lock = asyncio.Lock()
        self._cleanup: asyncio.Task[None] | None = None

    def _path(self, path: str) -> Path:
        if self._destroyed:
            raise RuntimeError("sandbox has been destroyed")
        relative = Path(path)
        resolved = (self._root / relative).resolve()
        if relative.is_absolute() or not resolved.is_relative_to(self._root):
            raise ValueError("sandbox path must stay within its workspace")
        return resolved

    async def exec(
        self, argv: Sequence[str], *, cwd: str = ".", env: Mapping[str, str] | None = None
    ) -> ProcessExecution:
        async with self._spawn_lock:
            directory = self._path(cwd)
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                *argv, cwd=directory,
                env={**os.environ, **(env or {}), **self._spec.secrets},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix",
            ))
            try:
                process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # Track a process even if cancellation arrives during creation,
                # so context teardown cannot miss it.
                process = await spawning
                self._track(process)
                raise
            return self._track(process)

    def _track(self, process: asyncio.subprocess.Process) -> ProcessExecution:
        execution = ProcessExecution(process, self._spec.timeout)
        self._executions.add(execution)

        def retire(_: asyncio.Task[int]) -> None:
            # wait() completes after pipe closure. Descendants that closed their
            # pipes still belong to us: stop them before forgetting the group.
            _kill(process)
            if execution._timer is not None:
                execution._timer.cancel()
            self._executions.discard(execution)

        execution._waiter.add_done_callback(retire)
        return execution

    async def copy_in(self, source: Path, destination: str) -> None:
        async with self._spawn_lock:
            await _filesystem_work(_copy, Path(source), self._path(destination))

    async def copy_out(self, source: str, destination: Path) -> None:
        async with self._spawn_lock:
            await _filesystem_work(_copy, self._path(source), Path(destination))

    async def destroy(self) -> None:
        if self._cleanup is None:
            self._destroyed = True
            self._cleanup = asyncio.create_task(self._destroy())
        cancelled = False
        while not self._cleanup.done():
            try:
                await asyncio.shield(self._cleanup)
            except asyncio.CancelledError:
                cancelled = True
        try:
            self._cleanup.result()
        finally:
            if cancelled:
                raise asyncio.CancelledError

    async def _destroy(self) -> None:
        async with self._spawn_lock:
            await self._stop_processes()

    async def _stop_processes(self) -> None:
        executions = tuple(self._executions)
        for execution in executions:
            if execution._timer is not None:
                execution._timer.cancel()
            _kill(execution.process)
        # Drain unread pipes so even commands blocked on output can be reaped.
        try:
            await asyncio.gather(*(
                execution.process.communicate() for execution in executions
            ), return_exceptions=True)
            await asyncio.gather(*(execution._waiter for execution in executions))
        finally:
            await _filesystem_work(_remove_workspace, self._root)


def _remove_workspace(root: Path) -> None:
    # Copies preserve directory modes. Restore owner access before traversal
    # and deletion, without changing permissions through preserved symlinks.
    def make_accessible(path: Path) -> None:
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(mode | stat.S_IRWXU)

    make_accessible(root)
    for directory, children, _ in os.walk(root, followlinks=False):
        for child in children:
            make_accessible(Path(directory) / child)
    shutil.rmtree(root)


def _copy(source: Path, destination: Path) -> None:
    # Never follow an existing destination link, including dangling links.
    if destination.is_symlink():
        raise ValueError("copy destination must not be a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(source.readlink(), target_is_directory=source.is_dir())
    elif source.is_dir():
        destination.mkdir(exist_ok=True)
        for child in source.iterdir():
            _copy(child, destination / child.name)
        shutil.copystat(source, destination)
    elif destination.is_dir():
        _copy(source, destination / source.name)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)


class ProcessSandbox:
    @asynccontextmanager
    async def create(self, spec: SandboxSpec) -> AsyncIterator[ProcessSandboxInstance]:
        if spec.image is not None or spec.egress_allowlist is not None:
            raise ValueError("process backend does not support images or egress restrictions")
        root = Path(tempfile.mkdtemp(prefix="engine-sandbox-")).resolve()
        sandbox = ProcessSandboxInstance(root, spec)
        try:
            if spec.workspace is not None:
                await sandbox.copy_in(spec.workspace, ".")
            yield sandbox
        finally:
            await sandbox.destroy()


__all__ = ["ProcessSandbox"]

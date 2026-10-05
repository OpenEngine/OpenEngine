"""Host execution with disposable workspace copies, not a security boundary.

Commands inherit the host environment, filesystem access and network access.
Images and egress restrictions are rejected rather than silently ignored.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
import os
from pathlib import Path
import shutil
import signal
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
        self._executions: list[ProcessExecution] = []
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
                self._executions.append(ProcessExecution(process, self._spec.timeout))
                raise
            execution = ProcessExecution(process, self._spec.timeout)
            self._executions.append(execution)
            return execution

    async def copy_in(self, source: Path, destination: str) -> None:
        _copy(Path(source), self._path(destination))

    async def copy_out(self, source: str, destination: Path) -> None:
        _copy(self._path(source), Path(destination))

    async def destroy(self) -> None:
        if self._cleanup is None:
            self._destroyed = True
            self._cleanup = asyncio.create_task(self._destroy())
        try:
            await asyncio.shield(self._cleanup)
        except asyncio.CancelledError:
            await self._cleanup
            raise

    async def _destroy(self) -> None:
        async with self._spawn_lock:
            await self._stop_processes()

    async def _stop_processes(self) -> None:
        for execution in self._executions:
            if execution._timer is not None:
                execution._timer.cancel()
            _kill(execution.process)
        # Drain unread pipes so even commands blocked on output can be reaped.
        try:
            await asyncio.gather(*(
                execution.process.communicate() for execution in self._executions
            ), return_exceptions=True)
            await asyncio.gather(*(execution._waiter for execution in self._executions))
        finally:
            shutil.rmtree(self._root)


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)


class ProcessSandbox:
    @asynccontextmanager
    async def create(self, spec: SandboxSpec) -> AsyncIterator[ProcessSandboxInstance]:
        if spec.image is not None or spec.egress_allowlist is not None:
            raise ValueError("process backend does not support images or egress restrictions")
        root = Path(tempfile.mkdtemp(prefix="engine-sandbox-")).resolve()
        sandbox = ProcessSandboxInstance(root, spec)
        try:
            if spec.workspace is not None:
                shutil.copytree(spec.workspace, root, dirs_exist_ok=True)
            yield sandbox
        finally:
            await sandbox.destroy()


__all__ = ["ProcessSandbox"]

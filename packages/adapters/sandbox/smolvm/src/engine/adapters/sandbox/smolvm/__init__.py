"""Disposable named microVMs through SmolVM's released CLI.

No host mounts or credential forwarding. Recursive transfers use archives,
since `machine cp` transfers single files rather than directory trees.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import tempfile
import uuid

from engine.ports import SandboxSpec

from . import _guest
from .support import SmolvmSupport, detect_support

# SmolVM cp targets the persistent overlay. /tmp is mounted as tmpfs by
# image exec and would hide uploaded files, so transfers live outside /workspace.
HELPER = "/oe-sandbox-transfer.py"
# The guest image supplies Python here; command PATH overrides must not affect
# the helper that validates that command's working directory and executable.
GUEST_PYTHON = "/usr/local/bin/python3"
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024


async def _settle(task: asyncio.Task):
    """Keep ownership through repeated cancellation; caller records the result."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


async def _filesystem(function, *args) -> None:
    _, cancelled = await _settle(asyncio.create_task(asyncio.to_thread(function, *args)))
    if cancelled:
        raise asyncio.CancelledError


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _discard_process_output(process: asyncio.subprocess.Process) -> None:
    async def drain(stream):
        if stream is not None:
            while await stream.read(65536):
                pass
    await asyncio.gather(drain(process.stdout), drain(process.stderr))
    await process.wait()


def _relative(path: str) -> str:
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        message = "sandbox path must stay within its workspace"
        raise ValueError(message)
    return str(relative)


class SmolvmError(RuntimeError):
    """A failed host-side SmolVM lifecycle or transfer command."""


class SmolvmProcess:
    def __init__(self, process: asyncio.subprocess.Process, reaper: asyncio.Task, instance: "SmolvmInstance"):
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr
        self._process = process
        self._reaper = reaper
        self._expired = False
        self._waiter = asyncio.create_task(process.wait())
        self._timer = None
        if instance.spec.timeout is not None:
            def expire():
                if process.returncode is None:
                    self._expired = True
                    # Killing just the CLI is insufficient: destroy the VM to
                    # ensure the guest command and its descendants also stop.
                    instance._begin_destroy()
            timer = asyncio.get_running_loop().call_later(instance.spec.timeout, expire)
            self._timer = timer
            self._waiter.add_done_callback(lambda _: timer.cancel())

    async def wait(self) -> int:
        status = await asyncio.shield(self._waiter)
        if self._expired:
            message = "sandbox command timed out"
            raise TimeoutError(message)
        # Settle the spawn reaper so the process is removed from
        # _processes when the command is done, not only on the next loop turn.
        await _settle(self._reaper)
        return status


class SmolvmInstance:
    def __init__(self, executable: str, name: str, spec: SandboxSpec):
        self.executable, self.name, self.spec = executable, name, spec
        self._lock = asyncio.Lock()
        self._destroyed = False
        self._cleanup: asyncio.Task | None = None
        self._processes: set[asyncio.subprocess.Process] = set()
        # Helpers are host commands only. Model/forge env is never forwarded
        # implicitly; explicit secret references are supplied per guest exec.
        self._environment = {key: os.environ[key] for key in (
            "PATH", "HOME", "TMPDIR", "XDG_DATA_HOME", "XDG_CACHE_HOME", "DYLD_LIBRARY_PATH",
            "SMOLVM_DATA_DIR", "SMOLVM_LIB_DIR", "SMOLVM_CONFIG",
        ) if key in os.environ}

    def _alive(self) -> None:
        if self._destroyed:
            message = "sandbox has been destroyed"
            raise RuntimeError(message)

    def _redact(self, message: str) -> str:
        # Replace longer values first so overlapping secrets stay fully hidden.
        for secret in sorted(self.spec.secrets.values(), key=len, reverse=True):
            if secret:
                message = message.replace(secret, "[redacted]")
        return message

    def _exec_arguments(self, command: Sequence[str], env: Mapping[str, str] | None = None):
        args = ["machine", "exec", "--name", self.name, "--interactive"]
        environment = dict(self._environment)
        for key, value in (env or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value:
                message = "invalid sandbox environment variable"
                raise ValueError(message)
            args.extend(["--env", f"{key}={value}"])
        for index, (key, value) in enumerate(self.spec.secrets.items()):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\0" in value:
                message = "invalid sandbox secret variable"
                raise ValueError(message)
            reference = f"OE_SANDBOX_SECRET_{index}"
            environment[reference] = value
            # The CLI's explicit --env wins over --secret-env. Exclude any
            # overlapping env key so the spec's secret remains authoritative.
            if env and key in env:
                position = args.index(f"{key}={env[key]}")
                del args[position - 1:position + 1]
            args.extend(["--secret-env", f"{key}={reference}"])
        return [*args, "--", *command], environment

    async def _spawn(self, arguments: Sequence[str], environment=None):
        task = asyncio.create_task(asyncio.create_subprocess_exec(
            self.executable, *arguments, env=environment or self._environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        ))
        process, cancelled = await _settle(task)
        self._processes.add(process)

        async def reap():
            await process.wait()
            self._processes.discard(process)

        reaper = asyncio.create_task(reap())
        if cancelled:
            raise asyncio.CancelledError
        # The caller settles the reaper so the process is removed from
        # _processes when this method's process is done, instead of a
        # done-callback firing after the return on a busy loop.
        return process, reaper

    async def _command(self, arguments, environment=None, *, check=True):
        process, reaper = await self._spawn(arguments, environment)
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        process.stdin.close()
        total = 0

        async def collect(stream):
            nonlocal total
            output = bytearray()
            while chunk := await stream.read(65536):
                total += len(chunk)
                if total > MAX_COMMAND_OUTPUT_BYTES:
                    message = "SmolVM command output exceeds byte budget"
                    raise SmolvmError(message)
                output.extend(chunk)
            return bytes(output)

        readers = [asyncio.create_task(collect(process.stdout)), asyncio.create_task(collect(process.stderr))]
        try:
            async with asyncio.timeout(300):
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
        finally:
            if process.returncode is None:
                _kill(process)

            async def finish():
                await asyncio.gather(*readers, return_exceptions=True)
                await _discard_process_output(process)
            await _settle(asyncio.create_task(finish()))
            await _settle(reaper)
        if check and process.returncode:
            detail = self._redact(stderr.decode(errors="replace").strip())
            message = f"smolvm {arguments[1]} failed ({process.returncode}): {detail}"
            raise SmolvmError(message)
        return stdout, stderr, process.returncode

    async def _helper(self, operation: str, *arguments: str, env=None):
        args, environment = self._exec_arguments([GUEST_PYTHON, HELPER, operation, *arguments], env)
        stdout, stderr, code = await self._command(args, environment, check=False)
        if code:
            try:
                failure = json.loads(stdout)
            except (ValueError, UnicodeError):
                detail = self._redact(stderr.decode(errors="replace").strip())
                message = f"guest transfer helper failed ({code}): {detail}"
                raise SmolvmError(message) from None
            # A guest may replace the helper and choose every JSON field.
            message = failure.get("message") if isinstance(failure, dict) else None
            message = self._redact(message) if isinstance(message, str) else "guest transfer failed"
            kind = failure.get("error") if isinstance(failure, dict) else None
            if kind == "FileNotFoundError":
                raise FileNotFoundError(message)
            if isinstance(kind, str) and kind in {"ValueError", "FilterError", "OutsideDestinationError", "AbsoluteLinkError", "LinkOutsideDestinationError"}:
                raise ValueError(message)
            raise SmolvmError(message)
        return stdout

    async def exec(self, argv: Sequence[str], *, cwd: str = ".", env: Mapping[str, str] | None = None) -> SmolvmProcess:
        async with self._lock:
            self._alive()
            directory = _relative(cwd)
            if not argv:
                message = "sandbox command must not be empty"
                raise ValueError(message)
            checked = json.loads(await self._helper("check", directory, argv[0], env=env))
            args, environment = self._exec_arguments([checked["executable"], *argv[1:]], env)
            args[2:2] = ["--workdir", checked["cwd"]]
            process, reaper = await self._spawn(args, environment)
            return SmolvmProcess(process, reaper, self)

    async def copy_in(self, source: Path, destination: str) -> None:
        async with self._lock:
            self._alive()
            relative = _relative(destination)
            guest_archive = f"/oe-transfer-{uuid.uuid4().hex}.tar"
            with tempfile.TemporaryDirectory(prefix="oe-transfer-") as temporary:
                archive = Path(temporary) / "transfer.tar"
                await _filesystem(_guest.pack, Path(source), archive)
                async with self._transfer_archive(guest_archive):
                    await self._command(["machine", "cp", str(archive), f"{self.name}:{guest_archive}"])
                    await self._helper("unpack", guest_archive, relative)

    async def copy_out(self, source: str, destination: Path) -> None:
        async with self._lock:
            self._alive()
            _guest._check_export_destination(Path(destination))
            relative = _relative(source)
            guest_archive = f"/oe-transfer-{uuid.uuid4().hex}.tar"
            with tempfile.TemporaryDirectory(prefix="oe-transfer-") as temporary:
                archive = Path(temporary) / "transfer.tar"
                async with self._transfer_archive(guest_archive):
                    await self._helper("pack", relative, guest_archive)
                    await self._receive_archive(guest_archive, archive)
                    await _filesystem(_guest.unpack, archive, Path(destination), True)

    @asynccontextmanager
    async def _transfer_archive(self, archive: str):
        primary = None
        try:
            yield
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                _, cancelled = await _settle(asyncio.create_task(self._helper("remove", archive)))
            except Exception as cleanup:
                if primary is None:
                    raise
                primary.add_note(f"guest archive cleanup failed: {cleanup}")
            else:
                if cancelled and primary is None:
                    raise asyncio.CancelledError

    async def _receive_archive(self, guest_archive: str, archive: Path) -> None:
        # Guest code can replace the helper or archive. Enforce the bound on
        # host-observed bytes, rather than trusting a guest-side size check.
        args, environment = self._exec_arguments([GUEST_PYTHON, HELPER, "stream", guest_archive])
        process, reaper = await self._spawn(args, environment)
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        process.stdin.close()

        async def drain_errors():
            detail = bytearray()
            while chunk := await process.stderr.read(65536):
                detail.extend(chunk[:max(0, 8192 - len(detail))])
            return detail

        errors = asyncio.create_task(drain_errors())
        try:
            async with asyncio.timeout(300):
                total = 0
                with archive.open("wb") as output:
                    while chunk := await process.stdout.read(65536):
                        total += len(chunk)
                        if total > _guest.MAX_ARCHIVE_BYTES:
                            message = "transfer archive exceeds byte budget"
                            raise ValueError(message)
                        output.write(chunk)
                code = await process.wait()
                detail = (await asyncio.shield(errors)).decode(errors="replace")
                if code:
                    detail = self._redact(detail)
                    message = f"guest archive stream failed ({code}): {detail}"
                    raise SmolvmError(message)
        finally:
            if process.returncode is None:
                _kill(process)
            async def drain_output():
                while await process.stdout.read(65536):
                    pass
            await _settle(asyncio.create_task(drain_output()))
            await _settle(reaper)
            await _settle(errors)

    def _begin_destroy(self):
        if self._cleanup is None:
            self._destroyed = True
            self._cleanup = asyncio.create_task(self._destroy())
        return self._cleanup

    async def destroy(self) -> None:
        _, cancelled = await _settle(self._begin_destroy())
        if cancelled:
            raise asyncio.CancelledError

    async def _destroy(self) -> None:
        async with self._lock:
            processes = tuple(self._processes)
            for process in processes:
                _kill(process)
            await asyncio.gather(*(_discard_process_output(process) for process in processes), return_exceptions=True)
            # -f stops a running machine and removes its persistent storage.
            # Do not suppress a deletion failure: it may mean a leaked VM.
            stdout, stderr, code = await self._command(
                ["machine", "delete", "--name", self.name, "--force"], check=False,
            )
            if code:
                # A create cancelled before registration has nothing to delete.
                # Confirm absence instead of ignoring arbitrary deletion errors.
                listing, _, _ = await self._command(["machine", "ls", "--json"])
                if any(machine["name"] == self.name for machine in json.loads(listing)):
                    message = f"SmolVM could not delete sandbox {self.name}"
                    raise SmolvmError(message)


class SmolvmSandbox:
    def __init__(self, image: str | None = None, *, executable: str = "smolvm"):
        self.image, self.executable = image, executable

    @asynccontextmanager
    async def create(self, spec: SandboxSpec) -> AsyncIterator[SmolvmInstance]:
        _guest.require_patched_python()
        if spec.egress_allowlist is not None:
            message = "SmolVM egress allowlists are not implemented yet (#677)"
            raise ValueError(message)
        image = spec.image or self.image
        if not image:
            message = "configure sandbox.image or provide SandboxSpec.image with the built guest image"
            raise ValueError(message)
        support, cancelled = await _settle(asyncio.create_task(asyncio.to_thread(detect_support, self.executable)))
        if cancelled:
            raise asyncio.CancelledError
        if not support.available:
            raise RuntimeError(support.reason)
        name = f"oe-sandbox-{uuid.uuid4().hex}"
        sandbox = SmolvmInstance(support.executable, name, spec)
        created = False
        try:
            arguments = ["machine", "create", "--name", name, "--image", image, "--net"]
            for key, value in spec.labels.items():
                if not key or "=" in key or "\0" in key + value:
                    message = "invalid sandbox label"
                    raise ValueError(message)
                arguments.extend(["--label", f"{key}={value}"])
            # Keep one workload container alive: cp and exec must address the
            # same mount namespace, even when an image's default CMD exits.
            arguments.extend(["--", "sleep", "infinity"])
            # Mark ownership before creation: cancellation may arrive after the
            # record was saved but before the CLI reports success.
            created = True
            await sandbox._command(arguments)
            await sandbox._command(["machine", "start", "--name", name])
            await sandbox._command(["machine", "cp", str(Path(_guest.__file__).resolve()), f"{name}:{HELPER}"])
            await sandbox._helper("init")
            if spec.workspace is not None:
                await sandbox.copy_in(spec.workspace, ".")
            yield sandbox
        finally:
            if created:
                await sandbox.destroy()


__all__ = ["SmolvmSandbox", "SmolvmError", "SmolvmSupport", "detect_support"]

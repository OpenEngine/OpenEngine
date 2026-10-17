"""Disposable named microVMs through SmolVM's released CLI.

No host mounts. Selected model credentials use a private tmpfs home. Transfers use archives,
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
import sys
import tempfile
import uuid

from engine.ports import SandboxSpec
from engine.ports.sandbox import SANDBOX_STREAM_LIMIT
from engine.ports.sandbox_tasks import filesystem_work as _filesystem, settle_task as _settle

from . import _guest, _sessions
from ._lifecycle import Ownership, LEASE
from .support import SmolvmSupport, detect_support

# SmolVM cp targets the persistent overlay. /tmp is mounted as tmpfs by
# image exec and would hide uploaded files, so transfers live outside /workspace.
HELPER = "/oe-sandbox-transfer.py"
# The guest image supplies Python here; command PATH overrides must not affect
# the helper that validates that command's working directory and executable.
GUEST_PYTHON = "/usr/local/bin/python3"
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
MAX_MACHINE_LIST_BYTES = 32 * 1024 * 1024
SETUP_HINT = "; install the prerequisites or opt out with engine vm use process (sandbox.backend = 'process'); see docs/sandbox.md"


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
    def __init__(self, process: asyncio.subprocess.Process, instance: "SmolvmInstance"):
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        self.stdin, self.stdout, self.stderr = process.stdin, process.stdout, process.stderr
        self._process = process
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
        return status


class SmolvmInstance:
    def __init__(self, executable: str, name: str, spec: SandboxSpec):
        self.executable, self.name, self.spec = executable, name, spec
        self._lock = asyncio.Lock()
        self._destroyed = False
        self._cleanup: asyncio.Task | None = None
        self._processes: set[asyncio.subprocess.Process] = set()
        self._home: str | None = None
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
        secrets = [*self.spec.secrets.values()]
        if self.spec.home is not None:
            secrets.extend((*self.spec.home.files.values(), *self.spec.home.all_redactions))
        for secret in sorted(secrets, key=len, reverse=True):
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
            limit=SANDBOX_STREAM_LIMIT,
        ))
        process, cancelled = await _settle(task)
        self._processes.add(process)
        waiter = asyncio.create_task(process.wait())
        waiter.add_done_callback(lambda _: self._processes.discard(process))
        if cancelled:
            raise asyncio.CancelledError
        return process

    async def _command(self, arguments, environment=None, *, check=True, input: bytes | None = None):
        process = await self._spawn(arguments, environment)
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        total = 0
        limit = MAX_MACHINE_LIST_BYTES if arguments[:2] == ["machine", "ls"] else MAX_COMMAND_OUTPUT_BYTES

        async def collect(stream):
            nonlocal total
            output = bytearray()
            while chunk := await stream.read(65536):
                total += len(chunk)
                if total > limit:
                    message = "SmolVM command output exceeds byte budget"
                    raise SmolvmError(message)
                output.extend(chunk)
            return bytes(output)

        readers = [asyncio.create_task(collect(process.stdout)), asyncio.create_task(collect(process.stderr))]
        try:
            async with asyncio.timeout(300):
                if input is not None:
                    process.stdin.write(input)
                process.stdin.close()
                stdout, stderr = await asyncio.gather(*readers)
                await process.wait()
            if check and process.returncode:
                detail = self._redact(stderr.decode(errors="replace").strip())
                message = f"smolvm {arguments[1]} failed ({process.returncode}): {detail}"
                raise SmolvmError(message)
            return stdout, stderr, process.returncode
        finally:
            primary = sys.exception()
            process.stdin.close()
            if process.returncode is None:
                _kill(process)

            async def finish():
                await asyncio.gather(*readers, return_exceptions=True)
                await _discard_process_output(process)
                # The detached waiter's done callback may run later. Once this
                # command is drained and reaped, retire it before returning.
                self._processes.discard(process)
            _, cancelled = await _settle(asyncio.create_task(finish()))
            if cancelled and primary is None:
                raise asyncio.CancelledError

    async def _helper(self, operation: str, *arguments: str, env=None, input: bytes | None = None):
        args, environment = self._exec_arguments([GUEST_PYTHON, HELPER, operation, *arguments], env)
        stdout, stderr, code = await self._command(args, environment, check=False, input=input)
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

    async def working_directory(self, path: str = ".") -> str:
        async with self._lock:
            self._alive()
            return json.loads(await self._helper("cwd", _relative(path)))

    async def exec(self, argv: Sequence[str], *, cwd: str = ".", env: Mapping[str, str] | None = None) -> SmolvmProcess:
        async with self._lock:
            self._alive()
            directory = _relative(cwd)
            if not argv:
                message = "sandbox command must not be empty"
                raise ValueError(message)
            checked = json.loads(await self._helper("check", directory, argv[0], env=env))
            # Pass names, not secret values, to the guest environment scrubber.
            keys = sorted(set(env or {}) | set(self.spec.secrets))
            args, environment = self._exec_arguments([
                GUEST_PYTHON, HELPER, "exec", directory, json.dumps(keys),
                *(["--home", self._home] if self._home is not None else []),
                checked["executable"], *argv[1:],
            ], env)
            args[2:2] = ["--workdir", checked["cwd"]]
            return SmolvmProcess(await self._spawn(args, environment), self)

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

    async def refresh_credentials(self, *, final: bool = False) -> None:
        home = self.spec.home
        if home is None or home.credentials is None:
            return
        if self._home is None:
            message = "guest credential home is unavailable"
            raise ValueError(message)
        state = home.credentials
        state.finalized = False
        try:
            raw = await self._helper("credentials", self._home, json.dumps(state.files))
        except Exception:  # noqa: BLE001 -- this secret channel must never echo guest error text
            message = "could not capture guest credentials; refusing export"
            raise ValueError(message) from None
        try:
            values = json.loads(raw)
            if not isinstance(values, dict) or set(values) != set(state.files) or any(
                value is not None and (not isinstance(value, str) or len(value.encode()) > 65536) for value in values.values()
            ):
                raise ValueError  # noqa: TRY301 -- reject malformed control snapshots
        except (ValueError, UnicodeError):
            message = "invalid guest credential snapshot"
            raise ValueError(message) from None
        await _filesystem(state.observe, values)
        if final:
            state.finalized = True

    async def _sessions_transfer(self, *, export: bool) -> None:
        profile = self.spec.sessions
        if profile is None:
            return
        if self._home is None:
            message = "persistent agent sessions require a private home"
            raise ValueError(message)
        state = profile.directory / "state.tar"
        if not export and not state.exists():
            return
        guest_archive = f"/oe-transfer-{uuid.uuid4().hex}.tar"
        # Staging must share a filesystem with state.tar for atomic replacement.
        with tempfile.TemporaryDirectory(prefix=".transfer-", dir=profile.directory) as temporary:
            root = Path(temporary)
            archive = root / "transfer.tar"
            async with self._transfer_archive(guest_archive):
                if export:
                    await self.refresh_credentials(final=True)
                    await self._helper("session-pack", self._home, profile.provider, guest_archive)
                    await self._receive_archive(guest_archive, archive)
                    await _filesystem(_sessions.checkpoint, archive, root, state, profile.provider,
                        self.spec.home.all_redactions if self.spec.home is not None else ())
                else:
                    if state.is_symlink():
                        message = "agent session checkpoint must not be a symlink"
                        raise ValueError(message)
                    # Validate again on restore; files may have survived a restart.
                    await _filesystem(_guest.unpack, state, root / "validated", True)
                    await _filesystem(_sessions.validate, root / "validated", profile.provider)
                    await self._command(["machine", "cp", str(state), f"{self.name}:{guest_archive}"])
                    await self._helper("session-unpack", self._home, profile.provider, guest_archive)

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
        process = await self._spawn(args, environment)
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        stdout, stderr = process.stdout, process.stderr
        process.stdin.close()

        async def drain_errors():
            detail = bytearray()
            while chunk := await stderr.read(65536):
                detail.extend(chunk[:max(0, 8192 - len(detail))])
            return detail

        errors = asyncio.create_task(drain_errors())
        try:
            async with asyncio.timeout(300):
                total = 0
                with archive.open("wb") as output:
                    while chunk := await stdout.read(65536):
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
            primary = sys.exception()
            if process.returncode is None:
                _kill(process)
            async def drain_output():
                while await stdout.read(65536):
                    pass
            cancelled = False
            for task in (asyncio.create_task(drain_output()), asyncio.create_task(process.wait()), errors):
                _, interrupted = await _settle(task)
                cancelled |= interrupted
            self._processes.discard(process)
            if cancelled and primary is None:
                raise asyncio.CancelledError

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
            if self._home is not None:
                try:
                    async with asyncio.timeout(5):
                        await self._helper("home-remove", self._home)
                except Exception:  # noqa: BLE001 -- a replaced helper must not prevent VM destruction
                    pass
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
    def __init__(self, image: str | None = None, *, executable: str = "smolvm", state_directory: str | Path | None = None):
        self.image, self.executable = image, executable
        self.ownership = Ownership(state_directory)

    async def sweep_orphans(self) -> tuple[str, ...]:
        """Reclaim only this deployment's VMs whose host lease is no longer held."""
        support, cancelled = await _settle(asyncio.create_task(asyncio.to_thread(detect_support, self.executable)))
        if cancelled:
            raise asyncio.CancelledError
        if not support.available or support.executable is None:
            raise SmolvmError(support.reason + SETUP_HINT)
        inspector = SmolvmInstance(support.executable, "recovery", SandboxSpec())
        listing, _, _ = await inspector._command(["machine", "ls", "--json"])
        machines = json.loads(listing)
        if not isinstance(machines, list) or not all(isinstance(machine, dict) for machine in machines):
            message = "invalid SmolVM machine listing during orphan recovery"
            raise SmolvmError(message)
        removed = []
        for machine in machines:
            identity = self.ownership.orphan(machine)
            if identity is None:
                continue
            with self.ownership.lock(identity) as lease:
                if lease is None:
                    continue
                await SmolvmInstance(support.executable, machine["name"], SandboxSpec()).destroy()
                lease.unlink(missing_ok=True)
                removed.append(machine["name"])
        self.ownership.prune()
        return tuple(removed)

    @asynccontextmanager
    async def create(self, spec: SandboxSpec) -> AsyncIterator[SmolvmInstance]:
        _guest.require_patched_python()
        image = spec.image or self.image
        if not image:
            message = "configure sandbox.image or provide SandboxSpec.image with the built guest image"
            raise ValueError(message)
        support, cancelled = await _settle(asyncio.create_task(asyncio.to_thread(detect_support, self.executable)))
        if cancelled:
            raise asyncio.CancelledError
        if not support.available or support.executable is None:
            raise RuntimeError(support.reason + SETUP_HINT)
        with self.ownership.create() as ownership:
            name = f"oe-sandbox-{ownership[LEASE]}"
            sandbox = SmolvmInstance(support.executable, name, spec)
            created = False
            try:
                arguments = ["machine", "create", "--name", name, "--image", image]
                if spec.egress_allowlist is None:
                    arguments.append("--net")
                elif spec.egress_allowlist:
                    # Host-side network stack enforces exact DNS grants. Legacy
                    # --allow-host also grants subdomains, which nodes did not ask for.
                    arguments.extend(["--net-backend", "virtio-net"])
                    for host in spec.egress_allowlist:
                        arguments.extend(["--allow-host-pattern", host])
                for mount in spec.sockets:
                    if mount.host.is_symlink() or not mount.host.is_socket():
                        message = "sandbox control endpoint must be an existing host Unix socket"
                        raise ValueError(message)
                    arguments.extend(["--mount-socket", f"{mount.host}:{mount.guest}"])
                for key, value in {**spec.labels, **ownership}.items():
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
                if spec.home is not None:
                    home = spec.home
                    sandbox._home = json.loads(await sandbox._helper("home", input=json.dumps({
                        "files": dict(home.files), "paths": dict(home.paths), "env_files": dict(home.env_files),
                    }).encode()))
                if spec.workspace is not None:
                    await sandbox.copy_in(spec.workspace, ".")
                await sandbox._sessions_transfer(export=False)
                try:
                    yield sandbox
                finally:
                    # The runtime's workspace wrapper has stopped the agent by
                    # this point. Teardown still runs if checkpointing fails.
                    primary = sys.exception()
                    async def save_private_state():
                        await sandbox.refresh_credentials(final=True)
                        await sandbox._sessions_transfer(export=True)
                    try:
                        _, cancelled = await _settle(asyncio.create_task(save_private_state()))
                    except Exception as cleanup:  # noqa: BLE001 -- preserve the node's original failure
                        if primary is None:
                            raise
                        primary.add_note(f"agent session checkpoint failed: {cleanup}")
                    else:
                        if cancelled and primary is None:
                            raise asyncio.CancelledError
            finally:
                if created:
                    await sandbox.destroy()


__all__ = ["SmolvmSandbox", "SmolvmError", "SmolvmSupport", "detect_support"]

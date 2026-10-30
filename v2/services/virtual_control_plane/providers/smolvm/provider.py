"""smolvm adapter behind the managed `AllocationProvider` port.

Persistent named microVMs through the released `smolvm machine` CLI
(`create`, `start`, `status`, `ls`, `exec`, `cp`). A machine's name is derived
from the persisted ownership and every machine VCP creates carries ownership
labels, so provisioning finds its own half-made machine after a crash and a
machine under that name with different labels is never started, prepared or
otherwise touched. Preparation runs inside the guest as idempotent steps
beneath `/workspace` and ends with a marker naming the runtime, so an
interrupted preparation resumes where it stopped. No host directory or
credential is mounted or forwarded. Service shutdown never destroys a machine.

Transient CLI failures (non-zero exits, timeouts) raise retryable `UNAVAILABLE`
errors for the lifecycle's capped backoff; ownership mismatches, missing tools
and vanished machines are terminal.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import signal
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any, NoReturn

from openengine.messaging import ErrorCode, ServiceError
from openengine.messaging.errors import invalid_argument

from virtual_control_plane.application.ports import Ownership, OwnershipMismatch, RuntimeContext
from virtual_control_plane.persistence.store import Locator

PROVIDER = "smolvm"
GUEST_ROOT = "/workspace"
GUEST_STATE = "/oe-vcp"
"""Adapter bookkeeping inside the guest, outside the workspace an agent sees."""
READY_MARKER = f"{GUEST_STATE}/ready"
CONTEXT_ARCHIVE = f"{GUEST_STATE}/context.tar"
INSTRUCTIONS_FILE = "AGENTS.md"
DEFAULT_TOOLS = ("git", "terminal")
DEFAULT_MAX_RESOURCES = {"cpu_millis": 8000, "memory_mib": 16384, "disk_gib": 100}
# The guest executable each advertised tool stands for; anything else is itself.
GUEST_TOOLS = {"terminal": "sh"}
COMMAND_TIMEOUT_SECONDS = 300
# Host variables the CLI needs to find its own data; nothing of the agent's.
FORWARDED_ENVIRONMENT = ("PATH", "HOME", "TMPDIR", "XDG_DATA_HOME", "XDG_CACHE_HOME", "DYLD_LIBRARY_PATH",
                         "SMOLVM_DATA_DIR", "SMOLVM_LIB_DIR", "SMOLVM_CONFIG")
MISSING_TOOL_EXIT = 3

log = logging.getLogger(__name__)


def machine_name(ownership: Ownership) -> str:
    """Deterministic: the same persisted runtime always names the same machine."""
    return f"oe-vcp-{ownership.runtime_id}"


def _labels(ownership: Ownership) -> dict[str, str]:
    return {"oe.workspace": ownership.workspace_id, "oe.runtime": ownership.runtime_id,
            "oe.generation": str(ownership.generation)}


def _unavailable(message: str, **details: Any) -> NoReturn:
    """Transient: the lifecycle retries the same ownership with capped backoff."""
    raise ServiceError(ErrorCode.UNAVAILABLE, message, retryable=True, details=details or None)


def _terminal(message: str, **details: Any) -> NoReturn:
    """Final for this operation; ownership is retained for explicit cleanup."""
    raise ServiceError(ErrorCode.PROVIDER_ERROR, message, retryable=False, details=details or None)


def _guest_path(path: str) -> str:
    relative = PurePosixPath(path)
    if not path or "\0" in path or relative.is_absolute() or any(part in ("..", "") for part in relative.parts):
        raise ValueError(path)
    return str(relative)


class Smolvm:
    """One adapter instance per configured image; it owns the host's `smolvm` CLI."""

    def __init__(self, image: str, *, tools: Sequence[str] = DEFAULT_TOOLS, executable: str = "smolvm",
                 max_resources: Mapping[str, int] = DEFAULT_MAX_RESOURCES,
                 command_timeout: float = COMMAND_TIMEOUT_SECONDS) -> None:
        self.image, self.executable, self.command_timeout = image, executable, command_timeout
        self.tools = tuple(dict.fromkeys(tools))
        self.max_resources = dict(max_resources)

    @property
    def capabilities(self) -> dict[str, Any]:
        # Honest: microVM isolation and the tools this host was configured
        # with; checkpoint, suspend and restore are not implemented.
        return {"provider": PROVIDER, "isolation": "microvm", "checkpoint": False, "suspend": False,
                "restore": "none", "approvals_survive_restore": False,
                "max_resources": dict(self.max_resources), "tools": list(self.tools)}

    # -- port -----------------------------------------------------------------

    def validate(self, specification: dict[str, Any]) -> None:
        spec = specification["spec"]
        violations = []
        if spec.get("resources", {}).get("cpuMillis", 1000) % 1000:
            violations.append(("payload.workspace.spec.resources.cpuMillis",
                               "smolvm allocates whole vCPUs; use a multiple of 1000"))
        for index, file in enumerate(spec.get("context", {}).get("files", [])):
            try:
                _guest_path(file["path"])
            except ValueError:
                violations.append((f"payload.workspace.spec.context.files[{index}].path",
                                   "must be a relative path staying beneath the workspace root"))
        if violations:
            raise invalid_argument(violations)

    async def provision(self, ownership: Ownership, specification: dict[str, Any]) -> Locator:
        name = machine_name(ownership)
        if await self._owned(ownership) is None:
            await self._run(*self._create_arguments(name, ownership, specification["spec"]))
        await self._start(name)
        await self._prepare(name, ownership, specification["spec"])
        return self._locator(ownership)

    async def ensure(self, ownership: Ownership, locator: Locator, specification: dict[str, Any]) -> Locator:
        name = machine_name(ownership)
        if locator and locator.get("machine") not in (None, name):
            _terminal(f"locator names machine {locator['machine']!r}, not the owned {name!r}", machine=name)
        if await self._owned(ownership) is None:
            # Never reprovision silently: the workspace's data would be gone.
            _terminal(f"owned machine {name!r} no longer exists", machine=name)
        await self._start(name)
        await self._prepare(name, ownership, specification["spec"])
        return self._locator(ownership)

    def context(self, ownership: Ownership, locator: Locator, specification: dict[str, Any]) -> RuntimeContext:
        context = specification["spec"].get("context", {})
        return RuntimeContext(
            root=GUEST_ROOT,
            working_directory="repositories/0" if context.get("repositories") else ".",
            instruction_files=(INSTRUCTIONS_FILE,) if context.get("instructions") else (),
        )

    # -- machines --------------------------------------------------------------

    def _locator(self, ownership: Ownership) -> Locator:
        return {"machine": machine_name(ownership), "image": self.image, "labels": _labels(ownership), "root": GUEST_ROOT}

    def _create_arguments(self, name: str, ownership: Ownership, spec: dict[str, Any]) -> list[str]:
        arguments = ["machine", "create", "--name", name]
        for key, value in _labels(ownership).items():
            arguments += ["--label", f"{key}={value}"]
        arguments += ["--image", self.image]
        resources = spec.get("resources", {})
        if "cpuMillis" in resources:
            arguments += ["--cpus", str(resources["cpuMillis"] // 1000)]
        if "memoryMib" in resources:
            arguments += ["--mem", str(resources["memoryMib"])]
        if "diskGib" in resources:
            arguments += ["--storage", str(resources["diskGib"])]
        # Outbound network for repository clones; the workload only keeps the guest up.
        return [*arguments, "--net", "--", "sleep", "infinity"]

    async def _machines(self) -> list[dict[str, Any]]:
        output, _, _ = await self._run("machine", "ls", "--json")
        try:
            machines = json.loads(output or b"[]")
        except ValueError:
            _unavailable("smolvm machine ls returned unreadable output")
        return machines if isinstance(machines, list) else []

    async def _owned(self, ownership: Ownership) -> dict[str, Any] | None:
        """The machine this ownership names, or None; a foreign one under that name is refused."""
        name = machine_name(ownership)
        for machine in await self._machines():
            if machine.get("name") == name:
                labels = machine.get("labels") or {}
                expected = _labels(ownership)
                if any(labels.get(key) != value for key, value in expected.items() if key != "oe.generation"):
                    raise OwnershipMismatch(PROVIDER, name)
                return machine
        return None

    async def _start(self, name: str) -> None:
        output, _, _ = await self._run("machine", "status", "--name", name, "--json")
        try:
            state = json.loads(output or b"{}").get("state")
        except (ValueError, AttributeError):
            state = None
        if state != "running":
            await self._run("machine", "start", "--name", name)

    # -- preparation -----------------------------------------------------------

    async def _prepare(self, name: str, ownership: Ownership, spec: dict[str, Any]) -> None:
        """Idempotent guest steps; the marker makes a repeated call a cheap no-op."""
        marker, _, _ = await self._exec(name, "sh", "-c", 'cat "$1" 2>/dev/null || true', "sh", READY_MARKER)
        if marker.decode(errors="replace").strip() == ownership.runtime_id:
            return
        context = spec.get("context", {})
        await self._check_tools(name, spec, context)
        await self._exec(name, "sh", "-c", 'mkdir -p "$1" "$2"', "sh", GUEST_ROOT, GUEST_STATE)
        for index, repository in enumerate(context.get("repositories", [])):
            destination = f"{GUEST_ROOT}/repositories/{index}"
            await self._exec(name, "sh", "-c", 'if [ -d "$1/.git" ]; then exit 0; fi; rm -rf "$1" && git clone -q -- "$2" "$1"',
                             "sh", destination, repository["url"])
            if ref := repository.get("ref"):
                # A branch, tag or commit; a branch only exists remotely right after the clone.
                await self._exec(name, "sh", "-c",
                                 'git -C "$1" checkout -q --detach "$2" -- 2>/dev/null || git -C "$1" checkout -q --detach "origin/$2" --',
                                 "sh", destination, ref)
        if archive := self._context_archive(context):
            await self._transfer(name, archive, CONTEXT_ARCHIVE)
            await self._exec(name, "sh", "-c", 'tar -xf "$1" -C "$2" && rm -f "$1"', "sh", CONTEXT_ARCHIVE, GUEST_ROOT)
        await self._exec(name, "sh", "-c", 'printf %s "$1" > "$2"', "sh", ownership.runtime_id, READY_MARKER)

    async def _check_tools(self, name: str, spec: dict[str, Any], context: dict[str, Any]) -> None:
        required = ["tar", *(GUEST_TOOLS.get(tool, tool) for tool in spec.get("tools", []))]
        if context.get("repositories"):
            required.append("git")
        script = f'for tool in "$@"; do command -v "$tool" >/dev/null 2>&1 || {{ printf %s "$tool"; exit {MISSING_TOOL_EXIT}; }}; done'
        output, _, code = await self._exec(name, "sh", "-c", script, "sh", *dict.fromkeys(required), check=False)
        if code == MISSING_TOOL_EXIT:
            tool = output.decode(errors="replace").strip()
            _terminal(f"image {self.image!r} lacks required tool {tool!r}", tool=tool, image=self.image)
        if code:
            _unavailable(f"smolvm machine exec exited with {code}", command="exec")

    @staticmethod
    def _context_archive(context: dict[str, Any]) -> bytes | None:
        files = list(context.get("files", []))
        if context.get("instructions"):
            files.append({"path": INSTRUCTIONS_FILE, "content": context["instructions"]})
        if not files:
            return None
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for file in files:
                content = file["content"].encode()
                info = tarfile.TarInfo(_guest_path(file["path"]))  # validated at acceptance too
                info.size, info.mode = len(content), 0o644
                archive.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    async def _transfer(self, name: str, content: bytes, destination: str) -> None:
        # `machine cp` takes a host path; keep the staging file private to this process.
        directory = os.environ.get("TMPDIR") or "/tmp"
        path = os.path.join(directory, f"oe-vcp-{name}-{os.getpid()}.tar")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as staged:
                staged.write(content)
            await self._run("machine", "cp", path, f"{name}:{destination}")
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    async def _exec(self, name: str, *command: str, check: bool = True) -> tuple[bytes, bytes, int]:
        return await self._run("machine", "exec", "--name", name, "--", *command, check=check)

    # -- CLI -------------------------------------------------------------------

    async def _run(self, *arguments: str, check: bool = True) -> tuple[bytes, bytes, int]:
        """Run one CLI command to completion, killing its whole process group on timeout or cancellation."""
        environment = {key: os.environ[key] for key in FORWARDED_ENVIRONMENT if key in os.environ}
        subcommand = " ".join(arguments[:2])
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable, *arguments, env=environment, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
        except OSError as error:
            _terminal(f"cannot execute {self.executable!r}: {error.strerror or error}", executable=self.executable)
        try:
            async with asyncio.timeout(self.command_timeout):
                output, errors = await process.communicate()
        except TimeoutError:
            _unavailable(f"smolvm {subcommand} timed out after {self.command_timeout:g}s", command=subcommand)
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode and check:
            # CLI diagnostics can carry repository URLs with credentials: keep them out of
            # public errors and log them only for local debugging.
            log.debug("smolvm %s exited with %d: %s", subcommand, process.returncode,
                      errors.decode(errors="replace")[:500])
            _unavailable(f"smolvm {subcommand} exited with {process.returncode}", command=subcommand)
        return output, errors, process.returncode or 0

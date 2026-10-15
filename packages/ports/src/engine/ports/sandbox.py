"""Disposable execution environments, independent of their host or VM backend.

Use ``async with backend.create(spec) as sandbox``. Exiting the context must
await destruction, including when the body raises or is cancelled. Paths in
exec/copy operations are relative to the sandbox workspace; copy endpoints
outside the sandbox are local paths. Copies include directories recursively.
"""

from asyncio import StreamReader, StreamWriter
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


# Bound a newline-delimited message without imposing asyncio's 64 KiB default.
SANDBOX_STREAM_LIMIT = 8 * 1024 * 1024
HOST_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "SystemRoot", "WINDIR",
    "COMSPEC", "PATHEXT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    "__CF_USER_TEXT_ENCODING", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
})


def command_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Allowlisted host defaults plus explicitly granted command variables."""
    import os

    return {**{key: value for key, value in os.environ.items() if key in HOST_ENV_ALLOWLIST}, **(env or {})}


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    image: str | None = None
    workspace: Path | None = None
    """Directory to copy into a disposable workspace; None starts empty."""
    secrets: Mapping[str, str] = field(default_factory=dict, repr=False)
    """Environment variables injected into every command."""
    egress_allowlist: tuple[str, ...] | None = None
    """None requests unrestricted egress; an empty tuple denies all egress."""
    timeout: float | None = None
    """Maximum seconds per command, including time before wait() is called."""
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        import math

        if self.timeout is not None and (
            not math.isfinite(self.timeout) or self.timeout <= 0
        ):
            raise ValueError("sandbox timeout must be finite and positive")


@runtime_checkable
class SandboxProcess(Protocol):
    stdin: StreamWriter
    stdout: StreamReader
    stderr: StreamReader

    async def wait(self) -> int:
        """Return exit status, or raise TimeoutError after the command timeout.

        Drain stdout and stderr concurrently with wait for unbounded output.
        """
        ...


@runtime_checkable
class SandboxInstance(Protocol):
    async def working_directory(self, path: str = ".") -> str:
        """Resolve a workspace-relative path for a guest protocol such as ACP."""
        ...

    async def exec(
        self, argv: Sequence[str], *, cwd: str = ".", env: Mapping[str, str] | None = None
    ) -> SandboxProcess:
        """Start an argv command without a shell, with piped binary stdio."""
        ...

    async def copy_in(self, source: Path, destination: str) -> None: ...

    async def copy_out(self, source: str, destination: Path) -> None: ...

    async def destroy(self) -> None:
        """Idempotently stop commands and release the disposable workspace."""
        ...


@runtime_checkable
class Sandbox(Protocol):
    def create(self, spec: SandboxSpec) -> AbstractAsyncContextManager[SandboxInstance]:
        """Create an environment on entry and always destroy it on exit."""
        ...


__all__ = ["Sandbox", "SandboxInstance", "SandboxProcess", "SandboxSpec"]

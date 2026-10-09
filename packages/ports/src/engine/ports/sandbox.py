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

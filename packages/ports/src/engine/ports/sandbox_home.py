"""Secret files for an isolated, memory-backed guest home, never a repository."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re


@dataclass(frozen=True, slots=True)
class SandboxHome:
    files: Mapping[str, str] = field(repr=False)
    """Home-relative secret files, delivered over stdin rather than argv or disk."""
    paths: Mapping[str, str] = field(default_factory=dict)
    """Environment variables pointing at directories relative to the private home."""
    env_files: Mapping[str, str] = field(default_factory=dict)
    """Environment variables read from a private file immediately before exec."""
    redactions: tuple[str, ...] = field(default=(), repr=False)

    credentials: "SandboxCredentialState | None" = field(default=None, repr=False)

    @property
    def all_redactions(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.redactions, *(self.credentials.redactions if self.credentials else ()))))

    def __post_init__(self) -> None:
        for path in (*self.files, *self.paths.values(), *self.env_files.values(), *(self.credentials.files if self.credentials else ())):
            relative = PurePosixPath(path)
            if relative.is_absolute() or not path or ".." in relative.parts or "\0" in path:
                message = "sandbox home paths must be relative and stay inside the private home"
                raise ValueError(message)
        for key in (*self.paths, *self.env_files):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                message = "invalid sandbox home environment variable"
                raise ValueError(message)
        if any(path not in self.files for path in self.env_files.values()):
            message = "sandbox home environment must reference an injected file"
            raise ValueError(message)


@dataclass(slots=True)
class SandboxCredentialState:
    """Host-only observer for bounded reads of selected guest credential files."""

    files: tuple[str, ...]
    observe: Callable[[Mapping[str, str | None]], None] = field(repr=False)
    redactions: tuple[str, ...] = field(default=(), repr=False)
    exposed: bool = False
    finalized: bool = False

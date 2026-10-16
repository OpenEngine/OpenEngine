"""A private host checkpoint for selected agent session directories."""
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class SandboxSessions:
    provider: Literal['codex', 'claude']
    directory: Path

    def __post_init__(self) -> None:
        if self.provider not in {'codex', 'claude'}:
            message = 'unsupported sandbox session provider'
            raise ValueError(message)

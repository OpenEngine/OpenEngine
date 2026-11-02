"""Journal location checks and first-setup intent; an established journal is never recreated.

`initializing.json` records the binding before the journal is migrated, so an
interrupted first setup resumes for the same binding. `binding.json` seals it
once recovery commits, before any listener opens. After that, a missing or
unreadable journal is a recovery failure, never an empty start.
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any

BINDING = "binding.json"
PENDING = "initializing.json"


class Refusal(StrEnum):
    OVERLAPPING = "journal and workspace root must be separate directories"
    SHARED = "journal directory must be private to the daemon user"
    OWNED = "another daemon process owns this journal"
    REBOUND = "journal binding differs from the provisioned runtime; fence before replacement"
    MISSING = "established runtime journal is missing; refusing empty recovery"
    EMPTY = "established runtime journal has no runtime record; refusing empty recovery"
    INTERRUPTED = "interrupted initialization was for another binding"
    UNINITIALIZED = "no sealed journal binding; first setup requires --initialize and an empty journal"
    UNFENCED = "cannot confirm an interrupted executor stopped; runtime stays unready"


class RecoveryRefused(RuntimeError):
    """Fail closed. `evidence` names the binding and journal state behind the decision."""

    def __init__(self, reason: Refusal, **evidence: Any) -> None:
        super().__init__(reason)
        self.reason, self.evidence = reason, evidence


def locations(journal: Path, root: Path) -> tuple[Path, Path]:
    """Resolve both locations; neither may contain the other, so task context cannot reach ownership state."""
    root = root.resolve(strict=True)
    journal.mkdir(mode=0o700, parents=True, exist_ok=True)
    journal = journal.resolve(strict=True)
    if not root.is_dir() or journal.is_relative_to(root) or root.is_relative_to(journal):
        raise RecoveryRefused(Refusal.OVERLAPPING, journal=str(journal), workspace_root=str(root))
    if journal.stat().st_mode & 0o077:
        raise RecoveryRefused(Refusal.SHARED, journal=str(journal))
    return journal, root


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class Initialization:
    def __init__(self, directory: Path, target: dict[str, Any], journal_file: Path, *, initialize: bool) -> None:
        self.directory, self.target = directory, target
        binding, pending = directory / BINDING, directory / PENDING
        self.established = binding.exists()
        evidence = {"target": target, "journal": str(journal_file)}
        if self.established:
            if _read(binding) != target:
                raise RecoveryRefused(Refusal.REBOUND, **evidence)
            if not journal_file.is_file():
                raise RecoveryRefused(Refusal.MISSING, **evidence)
        elif pending.exists():
            if _read(pending) != target:
                raise RecoveryRefused(Refusal.INTERRUPTED, **evidence)
        elif not initialize or journal_file.exists():
            raise RecoveryRefused(Refusal.UNINITIALIZED, **evidence)
        else:
            _write(pending, target)

    def finish(self) -> None:
        _write(self.directory / BINDING, self.target)
        # The sealed binding wins even if this unlink is lost in a crash.
        (self.directory / PENDING).unlink(missing_ok=True)

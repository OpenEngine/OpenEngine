"""Agent Runner capability.

Executes a coding agent against a prepared workspace. Codex is the intended
first implementation; Claude Code or any other agent satisfies the same shape.

The port returns a result rather than streaming into the engine on purpose --
the engine is synchronous and pure, so progress reporting belongs to the
runtime, not to `decide`.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from engine.domain.ids import AttemptId, WorkspaceId


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """Outcome of a single agent attempt."""

    attempt_id: AttemptId
    succeeded: bool
    summary: str
    changed_files: tuple[str, ...] = field(default=())


@runtime_checkable
class AgentRunner(Protocol):
    """Runs one agent attempt to completion."""

    async def run_attempt(
        self, attempt_id: AttemptId, workspace_id: WorkspaceId, prompt: str
    ) -> AttemptResult:
        ...

    async def cancel(self, attempt_id: AttemptId) -> None:
        """Best-effort cancellation. Safe to call on an already-finished attempt."""
        ...


__all__ = ["AgentRunner", "AttemptResult"]

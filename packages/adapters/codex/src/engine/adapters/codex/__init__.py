"""Agent Runner capability, backed by Codex.

Placeholder for Ticket 1. Satisfies `engine.ports.AgentRunner` structurally; no
process spawning, sandboxing, or output parsing yet.
"""

from engine.domain.ids import AttemptId, WorkspaceId
from engine.ports.agent_runner import AttemptResult


class CodexAgentRunner:
    """Runs Codex against a provisioned workspace.

    Implements `engine.ports.AgentRunner`.
    """

    def __init__(self, binary_path: str = "codex", timeout_seconds: float = 3600.0) -> None:
        self._binary_path = binary_path
        self._timeout_seconds = timeout_seconds

    async def run_attempt(
        self, attempt_id: AttemptId, workspace_id: WorkspaceId, prompt: str
    ) -> AttemptResult:
        raise NotImplementedError("Codex execution lands with the agent-runner ticket")

    async def cancel(self, attempt_id: AttemptId) -> None:
        raise NotImplementedError("Codex cancellation lands with the agent-runner ticket")


__all__ = ["CodexAgentRunner"]

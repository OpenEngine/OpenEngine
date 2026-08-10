"""Agent Runner capability, backed by Codex.

Placeholder for Ticket 1. Satisfies `engine.ports.AgentRunner` structurally; no
process spawning, sandboxing, or output parsing yet.
"""

from collections.abc import Sequence

from engine.domain.agents import AgentProfile
from engine.domain.chat import Message
from engine.domain.ids import AgentRunId, WorkspaceId
from engine.domain.tools import ToolSpec
from engine.ports.agent_runner import AgentTurn


class CodexAgentRunner:
    """Runs Codex against a provisioned workspace.

    Implements `engine.ports.AgentRunner`.
    """

    def __init__(self, binary_path: str = "codex", timeout_seconds: float = 3600.0) -> None:
        self._binary_path = binary_path
        self._timeout_seconds = timeout_seconds

    async def run_turn(
        self,
        agent_run_id: AgentRunId,
        profile: AgentProfile,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] = (),
        workspace_id: WorkspaceId | None = None,
    ) -> AgentTurn:
        raise NotImplementedError("Codex execution lands with the agent-runner ticket")

    async def cancel(self, agent_run_id: AgentRunId) -> None:
        raise NotImplementedError("Codex cancellation lands with the agent-runner ticket")


__all__ = ["CodexAgentRunner"]

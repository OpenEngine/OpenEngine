"""Run state: the engine's memory between events.

State is data, not behaviour. It is rebuilt by folding events, so it must stay
trivially serialisable -- no handles, no connections, no adapter objects.
"""

from dataclasses import dataclass, field
from enum import Enum

from engine.domain.ids import AgentRunId, RunId, TaskId, WorkspaceId


class RunPhase(Enum):
    """Coarse lifecycle position of a run."""

    PENDING = "pending"
    PREPARING_WORKSPACE = "preparing_workspace"
    RUNNING_AGENT = "running_agent"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunState:
    """Everything the engine needs to decide what happens next."""

    run_id: RunId
    task_id: TaskId
    phase: RunPhase = RunPhase.PENDING
    repository: str = ""
    prompt: str = ""
    workspace_id: WorkspaceId | None = None
    agent_runs: tuple[AgentRunId, ...] = field(default=())
    max_agent_runs: int = 3

    @property
    def agent_runs_remaining(self) -> int:
        return max(0, self.max_agent_runs - len(self.agent_runs))

    @property
    def is_terminal(self) -> bool:
        return self.phase in (RunPhase.SUCCEEDED, RunPhase.FAILED)


__all__ = ["RunPhase", "RunState"]

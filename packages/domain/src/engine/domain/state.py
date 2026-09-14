"""Run state: the WorkOrder row the graph engine's runs are listed by.

State is data, not behaviour: trivially serialisable, no handles, no
connections, no adapter objects. Execution truth lives in the graph engine's own
checkpoints -- what is here is identity, submission metadata and a lifecycle
projection the runtime repairs from that truth.
"""

from dataclasses import dataclass
from enum import Enum

from engine.domain.ids import (
    MilestoneId,
    RunId,
    TaskId,
    WorkflowId,
)


class RunPhase(Enum):
    """Coarse lifecycle position of a run."""

    SCHEDULED = "scheduled"
    PENDING = "pending"
    RUNNING_AGENT = "running_agent"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunOrigin:
    """Where a run was asked for, and where its progress is reported back.

    Provider-neutral on purpose. `channel` and `thread_id` are whatever the
    communications adapter addresses a conversation by -- a Slack channel and
    the timestamp of the message that started the thread, a Buzz room and a
    post -- and `author` is whoever asked, in that same vocabulary.

    Nothing here decides anything. It travels with the run so the runtime can
    answer in the place the request came from rather than in a channel
    configured once for everything.
    """

    channel: str = ""
    thread_id: str = ""
    author: str = ""


@dataclass(frozen=True, slots=True)
class RunState:
    """Everything the interface needs to list and open one run."""

    run_id: RunId
    task_id: TaskId
    workflow_id: WorkflowId
    milestone_id: MilestoneId | None = None
    phase: RunPhase = RunPhase.PENDING
    repository: str = ""
    prompt: str = ""
    name: str = ""
    failure_reason: str = ""
    origin: RunOrigin | None = None
    """The conversation this run was requested from, or ``None`` for the web."""

    @property
    def is_terminal(self) -> bool:
        return self.phase in (RunPhase.SUCCEEDED, RunPhase.FAILED)


__all__ = ["RunOrigin", "RunPhase", "RunState"]

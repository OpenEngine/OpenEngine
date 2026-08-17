"""Approvals: what an agent asked permission for, and what a human answered.

A provider that pauses mid-turn is asking a question a person has to answer, and
the pause outlives the HTTP connection that happened to be watching. So the
request is a durable record rather than a callback parameter: it is written
before anybody is shown it, and the answer is written before the provider is
told it.

`ApprovalKind` and `ApprovalDecision` live here rather than beside the runner
port because both are persisted. The vocabulary is provider-neutral on purpose
-- Codex's `acceptForSession` and Claude's `updatedPermissions` are two
spellings of one decision, and which one was made has to be readable years after
the CLI that asked has changed its wire format.

The request is kept in fields rather than in one rendered sentence. A stored
`command` can be compared, searched, and shown differently by a different
client; a stored "Run `pytest` in /workspace?" can only be printed.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from engine.domain.ids import AgentInstanceId, AgentRunId, ApprovalId


class ApprovalKind(Enum):
    """What the agent is asking the user to allow."""

    COMMAND_EXECUTION = "command_execution"
    FILE_CHANGE = "file_change"
    TOOL_USE = "tool_use"


class ApprovalDecision(Enum):
    """Provider-neutral decisions a user can make about an approval request."""

    ACCEPT = "accept"
    ACCEPT_FOR_SESSION = "accept_for_session"
    CANCEL = "cancel"


class ApprovalStatus(Enum):
    """Where a request got to.

    `INTERRUPTED` is the honest end for a request nobody can answer any more:
    the provider process that asked is gone -- killed, crashed, or left behind
    by a server restart -- so neither accepting nor cancelling would reach
    anything. It is distinct from a cancellation, which a person chose.
    """

    PENDING = "pending"
    DECIDED = "decided"
    INTERRUPTED = "interrupted"


class ApprovalDecisionSource(Enum):
    """Who produced the decision.

    A session grant is a decision the user made earlier, applied again without
    asking. Recording the difference is what keeps an audit trail able to answer
    "who allowed this?" once such grants exist.
    """

    USER = "user"
    SESSION_GRANT = "session_grant"


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """One request for consent, and its outcome.

    Bound to the agent run that raised it, not merely to the conversation: a
    decision belongs to the provider process that is waiting for it, and a
    request from a run that has since ended must not be answerable.
    """

    approval_id: ApprovalId
    agent_run_id: AgentRunId
    instance_id: AgentInstanceId
    runner: str
    """Which runner asked, by the name the composition root gave it."""
    kind: ApprovalKind
    requested_at: datetime
    reason: str | None = None
    command: str | None = None
    cwd: str | None = None
    tool_name: str | None = None
    arguments: str | None = None
    """The tool's arguments as the provider stated them, unparsed.

    Text for the same reason `ToolCall.arguments` is: the domain owns no JSON
    codec, and arguments we cannot parse still have to be shown to the person
    being asked to approve them.
    """
    allowed_decisions: tuple[ApprovalDecision, ...] = field(default=())
    """What this particular request may be answered with.

    Provider-specific and not merely cosmetic: Claude offers a session grant
    only when it suggests permission rules to remember, and answering with a
    decision that was never offered is a protocol error rather than a strictness
    we can relax.
    """
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: ApprovalDecision | None = None
    decision_source: ApprovalDecisionSource | None = None
    decided_at: datetime | None = None

    @property
    def is_pending(self) -> bool:
        return self.status is ApprovalStatus.PENDING

    @property
    def is_resolved(self) -> bool:
        return not self.is_pending


__all__ = [
    "ApprovalDecision",
    "ApprovalDecisionSource",
    "ApprovalKind",
    "ApprovalRecord",
    "ApprovalStatus",
]

"""State Store capability.

Durable persistence of run state, agent identity, and conversation history.
Postgres is the intended first implementation; an in-memory dict satisfies it
for tests.

`append_events` plus `load` is deliberately event-sourcing-shaped: state can
always be rebuilt by folding history through `engine.core.decide`.

Conversations live here rather than inside a model provider's session. An
adapter may keep a native session for efficiency, but if the store is not the
source of truth then history cannot be resumed after a restart, inspected by a
human, or moved to a different provider.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from engine.domain.agents import AgentInstance, AgentRun
from engine.domain.approvals import ApprovalRecord, ApprovalStatus, SessionGrant
from engine.domain.chat import Conversation, Message
from engine.domain.events import Event
from engine.domain.ids import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    ApprovalId,
    ConversationId,
    RunId,
    StepId,
    TaskId,
    WorkspaceId,
)
from engine.domain.state import RunState


@runtime_checkable
class StateStore(Protocol):
    """Persists run state, the events that produced it, and agent history."""

    async def load(self, run_id: RunId) -> RunState | None:
        """Return the stored state, or None if the run is unknown."""
        ...

    async def save(self, state: RunState) -> None:
        ...

    async def list_runs(self) -> Sequence[RunState]:
        """Return persisted workflow runs, newest first."""
        ...

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        ...

    async def history(self, run_id: RunId) -> Sequence[Event]:
        ...

    # --- agent identity and conversation ---------------------------------

    async def create_instance(
        self,
        agent_id: AgentId,
        task_id: TaskId | None = None,
        workspace_id: WorkspaceId | None = None,
        runner: str = "",
        *,
        instance_id: AgentInstanceId | None = None,
        conversation_id: ConversationId | None = None,
        workflow_run_id: RunId | None = None,
        workflow_step_id: StepId | None = None,
    ) -> AgentInstance:
        """Start a durable instance of an agent role, with an empty conversation.

        The store mints both ids, so an instance and its conversation cannot
        exist without each other. A caller-provisioned workspace may be attached
        atomically at creation.
        """
        ...

    async def update_instance_metadata(
        self,
        instance_id: AgentInstanceId,
        title: str,
        archived: bool,
        runner: str,
        auto_approve: bool = False,
    ) -> AgentInstance:
        """Persist the user-facing state of an interactive instance."""
        ...

    async def load_instance(self, instance_id: AgentInstanceId) -> AgentInstance | None:
        ...

    async def attach_workspace(
        self, instance_id: AgentInstanceId, workspace_id: WorkspaceId | None
    ) -> AgentInstance:
        """Point an instance at a workspace, or at none.

        A conversation may be given one it was never created with, and keeps it
        across restarts. Returns the updated instance; raises `KeyError` if it
        is unknown.
        """
        ...

    async def list_instances(
        self,
        agent_id: AgentId | None = None,
        *,
        workflow_run_id: RunId | None = None,
    ) -> Sequence[AgentInstance]:
        """Every instance, or every instance of one role. Newest first."""
        ...

    async def load_conversation(self, instance_id: AgentInstanceId) -> Conversation | None:
        """The instance's history, or None if the instance is unknown.

        Keyed by instance rather than conversation id: an instance owns exactly
        one conversation, and callers hold the instance.
        """
        ...

    async def append_messages(
        self, instance_id: AgentInstanceId, messages: Sequence[Message]
    ) -> None:
        """Append to the instance's conversation, in order. The store assigns
        each message an id."""
        ...

    async def record_agent_run(self, agent_run: AgentRun) -> None:
        """Upsert one execution's status and outcome, keyed by its id."""
        ...

    # --- approvals --------------------------------------------------------

    async def record_approval(self, approval: ApprovalRecord) -> None:
        """Upsert one request for consent, keyed by its id.

        Durable rather than held in the process that is waiting, because the
        pause outlives the connection that showed it and the answer has to be
        auditable after the provider is gone.
        """
        ...

    async def load_approval(self, approval_id: ApprovalId) -> ApprovalRecord | None:
        ...

    async def list_approvals(
        self,
        *,
        instance_id: AgentInstanceId | None = None,
        agent_run_id: AgentRunId | None = None,
        status: ApprovalStatus | None = None,
    ) -> Sequence[ApprovalRecord]:
        """Persisted approvals, oldest first, narrowed by whatever is given.

        Oldest first because these are a log of what was asked: reading them in
        the order the agent asked is what makes a sequence of requests legible.
        """
        ...

    async def record_session_grant(self, grant: SessionGrant) -> None:
        """Upsert one reusable consent, keyed by its id.

        Durable for the reason the approval it came from is: the provider
        process that was told about it dies at the end of the turn, so a grant
        held anywhere else would expire before the next request it is meant to
        answer.
        """
        ...

    async def list_session_grants(
        self, *, instance_id: AgentInstanceId | None = None
    ) -> Sequence[SessionGrant]:
        """Persisted grants, oldest first, optionally for one conversation.

        Revoked ones are included: whether a grant still applies is a question
        about `revoked_at`, and a caller auditing why a past request was allowed
        needs the ones that have since been withdrawn.
        """
        ...


__all__ = ["StateStore"]

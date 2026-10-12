"""State Store capability, backed by Postgres.

The adapter satisfies `engine.ports.StateStore` structurally, but its behavior
and Alembic schema are placeholders until PostgreSQL support is needed.
"""

from collections.abc import Mapping, Sequence

from engine.domain.agents import AgentInstance, AgentRun
from engine.domain.approvals import ApprovalRecord, ApprovalStatus, SessionGrant
from engine.domain.chat import Conversation, Message
from engine.domain.ids import (
    AgentId,
    AgentInstanceId,
    AgentRunId,
    ApprovalId,
    ConversationId,
    RunId,
    TaskId,
    WorkOrderId,
    WorkspaceId,
)
from engine.domain.state import RunState
from engine.domain.scoping import (
    LoopQueueItem,
    PersistedScopingPlan,
    ScopingPlan,
    TicketApproval,
)


class PostgresStateStore:
    """Persists run state, agent identity, and conversations in Postgres.

    Implements `engine.ports.StateStore`.
    """

    def __init__(self, dsn: str, schema: str = "engine") -> None:
        # TODO: Implement PostgreSQL storage when OpenEngine has a need for it.
        self._dsn = dsn
        self._schema = schema

    async def save_scoping_plan(
        self, loop_id: str, plan: ScopingPlan
    ) -> PersistedScopingPlan:
        """Persist one proposal atomically, resolving all local references."""
        raise NotImplementedError("Scoped tickets require SQLite or memory")

    async def load_scoping_plan(self, plan_id: str) -> PersistedScopingPlan | None:
        raise NotImplementedError("Scoped tickets require SQLite or memory")

    async def set_ticket_approval(
        self, plan_id: str, ticket_id: WorkOrderId, approval: TicketApproval
    ) -> PersistedScopingPlan:
        """Update approval; the queue projection immediately reflects it."""
        raise NotImplementedError("Scoped tickets require SQLite or memory")

    async def list_loop_queue(self, loop_id: str) -> Sequence[LoopQueueItem]:
        """Approved tickets in proposal order, retaining dependency ids."""
        raise NotImplementedError("Scoped tickets require SQLite or memory")

    async def load(self, run_id: RunId) -> RunState | None:
        raise NotImplementedError("Postgres reads land with the state-store ticket")

    async def save(self, state: RunState) -> None:
        raise NotImplementedError("Postgres writes land with the state-store ticket")

    async def list_runs(self) -> Sequence[RunState]:
        raise NotImplementedError("Postgres reads land with the state-store ticket")

    async def list_runs_for_origin(
        self, channel: str, thread_id: str
    ) -> Sequence[RunState]:
        raise NotImplementedError("Postgres reads land with the state-store ticket")

    async def delete_run(self, run_id: RunId) -> bool:
        raise NotImplementedError("Postgres writes land with the state-store ticket")


    async def create_instance(
        self,
        agent_id: AgentId,
        task_id: TaskId | None = None,
        workspace_id: WorkspaceId | None = None,
        runner: str = "",
        *,
        instance_id: AgentInstanceId | None = None,
        conversation_id: ConversationId | None = None,
    ) -> AgentInstance:
        raise NotImplementedError("Agent instances land with the state-store ticket")

    async def update_instance_metadata(
        self,
        instance_id: AgentInstanceId,
        title: str,
        archived: bool,
        runner: str,
    ) -> AgentInstance:
        raise NotImplementedError(
            "Agent instance metadata lands with the state-store ticket"
        )

    async def load_instance(self, instance_id: AgentInstanceId) -> AgentInstance | None:
        raise NotImplementedError("Agent instances land with the state-store ticket")

    async def attach_workspace(
        self, instance_id: AgentInstanceId, workspace_id: WorkspaceId | None
    ) -> AgentInstance:
        raise NotImplementedError("Agent instances land with the state-store ticket")

    async def list_instances(
        self, agent_id: AgentId | None = None
    ) -> Sequence[AgentInstance]:
        raise NotImplementedError("Agent instances land with the state-store ticket")

    async def load_conversation(self, instance_id: AgentInstanceId) -> Conversation | None:
        raise NotImplementedError("Conversation reads land with the state-store ticket")

    async def load_conversations(
        self, instance_ids: Sequence[AgentInstanceId]
    ) -> Mapping[AgentInstanceId, Conversation]:
        raise NotImplementedError("Conversation reads land with the state-store ticket")

    async def append_messages(
        self, instance_id: AgentInstanceId, messages: Sequence[Message]
    ) -> None:
        raise NotImplementedError("Conversation writes land with the state-store ticket")

    async def record_agent_run(self, agent_run: AgentRun) -> None:
        raise NotImplementedError("Agent run records land with the state-store ticket")

    async def record_approval(self, approval: ApprovalRecord) -> None:
        raise NotImplementedError("Approval records land with the state-store ticket")

    async def load_approval(self, approval_id: ApprovalId) -> ApprovalRecord | None:
        raise NotImplementedError("Approval reads land with the state-store ticket")

    async def list_approvals(
        self,
        *,
        instance_id: AgentInstanceId | None = None,
        agent_run_id: AgentRunId | None = None,
        status: ApprovalStatus | None = None,
    ) -> Sequence[ApprovalRecord]:
        raise NotImplementedError("Approval reads land with the state-store ticket")

    async def record_session_grant(self, grant: SessionGrant) -> None:
        raise NotImplementedError("Session grants land with the state-store ticket")

    async def list_session_grants(
        self, *, instance_id: AgentInstanceId | None = None
    ) -> Sequence[SessionGrant]:
        raise NotImplementedError("Session grants land with the state-store ticket")


__all__ = ["PostgresStateStore"]

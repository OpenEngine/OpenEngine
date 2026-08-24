"""State Store capability, backed by Postgres.

The adapter satisfies `engine.ports.StateStore` structurally, but its behavior
and Alembic schema are placeholders until PostgreSQL support is needed.
"""

from collections.abc import Sequence

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
    MilestoneId,
    ProjectId,
    RunId,
    StepId,
    TaskId,
    WorkstreamId,
    WorkspaceId,
)
from engine.domain.planning import Milestone, Project, Workstream
from engine.domain.state import RunState


class PostgresStateStore:
    """Persists run state, agent identity, and conversations in Postgres.

    Implements `engine.ports.StateStore`.
    """

    def __init__(self, dsn: str, schema: str = "engine") -> None:
        # TODO: Implement PostgreSQL storage when OpenEngine has a need for it.
        self._dsn = dsn
        self._schema = schema

    async def load(self, run_id: RunId) -> RunState | None:
        raise NotImplementedError("Postgres reads land with the state-store ticket")

    async def save(self, state: RunState) -> None:
        raise NotImplementedError("Postgres writes land with the state-store ticket")

    async def list_runs(
        self, workstream_id: WorkstreamId | None = None
    ) -> Sequence[RunState]:
        raise NotImplementedError("Postgres reads land with the state-store ticket")

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        raise NotImplementedError("Event append lands with the state-store ticket")

    async def history(self, run_id: RunId) -> Sequence[Event]:
        raise NotImplementedError("History reads land with the state-store ticket")

    async def save_project(self, project: Project) -> None:
        raise NotImplementedError("Project writes land with the state-store ticket")

    async def load_project(self, project_id: ProjectId) -> Project | None:
        raise NotImplementedError("Project reads land with the state-store ticket")

    async def list_projects(self) -> Sequence[Project]:
        raise NotImplementedError("Project reads land with the state-store ticket")

    async def save_milestone(self, milestone: Milestone) -> None:
        raise NotImplementedError("Milestone writes land with the state-store ticket")

    async def load_milestone(self, milestone_id: MilestoneId) -> Milestone | None:
        raise NotImplementedError("Milestone reads land with the state-store ticket")

    async def list_milestones(
        self, project_id: ProjectId | None = None
    ) -> Sequence[Milestone]:
        raise NotImplementedError("Milestone reads land with the state-store ticket")

    async def delete_milestone(self, milestone_id: MilestoneId) -> bool:
        raise NotImplementedError("Milestone writes land with the state-store ticket")

    async def save_workstream(self, workstream: Workstream) -> None:
        raise NotImplementedError("Workstream writes land with the state-store ticket")

    async def load_workstream(self, workstream_id: WorkstreamId) -> Workstream | None:
        raise NotImplementedError("Workstream reads land with the state-store ticket")

    async def list_workstreams(
        self, milestone_id: MilestoneId | None = None
    ) -> Sequence[Workstream]:
        raise NotImplementedError("Workstream reads land with the state-store ticket")

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
        raise NotImplementedError("Agent instances land with the state-store ticket")

    async def update_instance_metadata(
        self,
        instance_id: AgentInstanceId,
        title: str,
        archived: bool,
        runner: str,
        auto_approve: bool = False,
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
        self,
        agent_id: AgentId | None = None,
        *,
        workflow_run_id: RunId | None = None,
    ) -> Sequence[AgentInstance]:
        raise NotImplementedError("Agent instances land with the state-store ticket")

    async def load_conversation(self, instance_id: AgentInstanceId) -> Conversation | None:
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

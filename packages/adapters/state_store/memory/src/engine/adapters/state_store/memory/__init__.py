"""State Store capability, held in process.

Unlike its Postgres sibling this one is finished: every method does what it says.
What it does not do is survive the process, which is the whole of the difference
between the two and the reason both exist. Chatting with an agent needs a real
store today; nothing about the conversation needs to outlive a restart yet.

It is also the honest test double. A fake that only pretends to store things
lets a caller's ordering bug pass unnoticed, so this keeps insertion order,
mints ids, and rejects writes against instances it never created -- the same
things Postgres will do.

Implements `engine.ports.StateStore`.
"""

from collections.abc import Sequence
from dataclasses import replace
from itertools import count
from threading import Lock
from uuid import uuid4

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
    MessageId,
    MilestoneId,
    ProjectId,
    RunId,
    SessionGrantId,
    StepId,
    TaskId,
    WorkstreamId,
    WorkspaceId,
)
from engine.domain.planning import Milestone, Project, Workstream
from engine.domain.state import RunState


class InMemoryStateStore:
    """Everything in dicts, guarded by a lock.

    A plain `threading.Lock` rather than an `asyncio.Lock`: callers reach this
    through `asyncio.run`, which means a fresh event loop per call, and an
    asyncio primitive bound to a dead loop is a bug waiting for the second
    caller. No method awaits while holding it.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._states: dict[RunId, RunState] = {}
        self._events: dict[RunId, list[Event]] = {}
        self._projects: dict[ProjectId, Project] = {}
        self._milestones: dict[MilestoneId, Milestone] = {}
        self._workstreams: dict[WorkstreamId, Workstream] = {}
        self._instances: dict[AgentInstanceId, AgentInstance] = {}
        self._conversations: dict[AgentInstanceId, Conversation] = {}
        self._agent_runs: dict[AgentRunId, AgentRun] = {}
        self._approvals: dict[ApprovalId, ApprovalRecord] = {}
        self._session_grants: dict[SessionGrantId, SessionGrant] = {}
        self._message_numbers = count(1)

    # --- runs ------------------------------------------------------------

    async def load(self, run_id: RunId) -> RunState | None:
        with self._lock:
            return self._states.get(run_id)

    async def save(self, state: RunState) -> None:
        with self._lock:
            if (
                state.workstream_id is not None
                and state.workstream_id not in self._workstreams
            ):
                raise KeyError(f"no workstream {state.workstream_id!r}")
            self._states[state.run_id] = state

    async def list_runs(
        self, workstream_id: WorkstreamId | None = None
    ) -> Sequence[RunState]:
        with self._lock:
            states = list(self._states.values())
        if workstream_id is not None:
            states = [state for state in states if state.workstream_id == workstream_id]
        return tuple(reversed(states))

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        with self._lock:
            self._events.setdefault(run_id, []).extend(events)

    async def history(self, run_id: RunId) -> Sequence[Event]:
        with self._lock:
            return tuple(self._events.get(run_id, ()))

    # --- planning hierarchy ---------------------------------------------

    async def save_project(self, project: Project) -> None:
        with self._lock:
            self._projects[project.project_id] = project

    async def load_project(self, project_id: ProjectId) -> Project | None:
        with self._lock:
            return self._projects.get(project_id)

    async def list_projects(self) -> Sequence[Project]:
        with self._lock:
            return tuple(reversed(self._projects.values()))

    async def save_milestone(self, milestone: Milestone) -> None:
        with self._lock:
            if milestone.project_id not in self._projects:
                raise KeyError(f"no project {milestone.project_id!r}")
            self._milestones[milestone.milestone_id] = milestone

    async def load_milestone(self, milestone_id: MilestoneId) -> Milestone | None:
        with self._lock:
            return self._milestones.get(milestone_id)

    async def list_milestones(
        self, project_id: ProjectId | None = None
    ) -> Sequence[Milestone]:
        with self._lock:
            milestones = list(self._milestones.values())
        if project_id is not None:
            milestones = [item for item in milestones if item.project_id == project_id]
        return tuple(reversed(milestones))

    async def delete_milestone(self, milestone_id: MilestoneId) -> bool:
        with self._lock:
            if any(
                workstream.milestone_id == milestone_id
                for workstream in self._workstreams.values()
            ):
                raise ValueError(f"milestone {milestone_id!r} still has workstreams")
            return self._milestones.pop(milestone_id, None) is not None

    async def save_workstream(self, workstream: Workstream) -> None:
        with self._lock:
            if workstream.milestone_id not in self._milestones:
                raise KeyError(f"no milestone {workstream.milestone_id!r}")
            self._workstreams[workstream.workstream_id] = workstream

    async def load_workstream(self, workstream_id: WorkstreamId) -> Workstream | None:
        with self._lock:
            return self._workstreams.get(workstream_id)

    async def list_workstreams(
        self, milestone_id: MilestoneId | None = None
    ) -> Sequence[Workstream]:
        with self._lock:
            workstreams = list(self._workstreams.values())
        if milestone_id is not None:
            workstreams = [
                item for item in workstreams if item.milestone_id == milestone_id
            ]
        return tuple(reversed(workstreams))

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
        instance = AgentInstance(
            instance_id=instance_id or AgentInstanceId(f"agi-{uuid4().hex[:12]}"),
            agent_id=agent_id,
            conversation_id=conversation_id
            or ConversationId(f"conv-{uuid4().hex[:12]}"),
            task_id=task_id,
            workspace_id=workspace_id,
            runner=runner,
            workflow_run_id=workflow_run_id,
            workflow_step_id=workflow_step_id,
        )
        with self._lock:
            existing = self._instances.get(instance.instance_id)
            if existing is not None:
                return existing
            self._instances[instance.instance_id] = instance
            # An instance without its conversation is a state no reader should
            # have to handle, so the two are created together or not at all.
            self._conversations[instance.instance_id] = Conversation(
                conversation_id=instance.conversation_id,
                instance_id=instance.instance_id,
            )
        return instance

    async def update_instance_metadata(
        self,
        instance_id: AgentInstanceId,
        title: str,
        archived: bool,
        runner: str,
        auto_approve: bool = False,
    ) -> AgentInstance:
        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None:
                raise KeyError(f"no agent instance {instance_id!r}")
            updated = replace(
                instance,
                title=title,
                archived=archived,
                runner=runner,
                auto_approve=auto_approve,
            )
            self._instances[instance_id] = updated
        return updated

    async def load_instance(self, instance_id: AgentInstanceId) -> AgentInstance | None:
        with self._lock:
            return self._instances.get(instance_id)

    async def attach_workspace(
        self, instance_id: AgentInstanceId, workspace_id: WorkspaceId | None
    ) -> AgentInstance:
        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None:
                raise KeyError(f"no agent instance {instance_id!r}")
            updated = replace(instance, workspace_id=workspace_id)
            self._instances[instance_id] = updated
        return updated

    async def list_instances(
        self,
        agent_id: AgentId | None = None,
        *,
        workflow_run_id: RunId | None = None,
    ) -> Sequence[AgentInstance]:
        with self._lock:
            instances = list(self._instances.values())
        if agent_id is not None:
            instances = [i for i in instances if i.agent_id == agent_id]
        if workflow_run_id is not None:
            instances = [i for i in instances if i.workflow_run_id == workflow_run_id]
        return tuple(reversed(instances))  # newest first

    async def load_conversation(self, instance_id: AgentInstanceId) -> Conversation | None:
        with self._lock:
            return self._conversations.get(instance_id)

    async def append_messages(
        self, instance_id: AgentInstanceId, messages: Sequence[Message]
    ) -> None:
        with self._lock:
            conversation = self._conversations.get(instance_id)
            if conversation is None:
                # An integrity check, not a lookup: callers resolve the instance
                # first, so reaching here means messages were about to pile up
                # somewhere no reader would ever look.
                raise KeyError(f"no agent instance {instance_id!r}")
            stamped = tuple(
                message.with_id(MessageId(f"msg-{next(self._message_numbers):06d}"))
                for message in messages
            )
            self._conversations[instance_id] = conversation.appending(*stamped)

    async def record_agent_run(self, agent_run: AgentRun) -> None:
        with self._lock:
            self._agent_runs[agent_run.agent_run_id] = agent_run

    # --- approvals --------------------------------------------------------

    async def record_approval(self, approval: ApprovalRecord) -> None:
        with self._lock:
            if approval.instance_id not in self._instances:
                # The same integrity check `append_messages` makes: an approval
                # nobody can reach from a conversation is one no reviewer will
                # ever find.
                raise KeyError(f"no agent instance {approval.instance_id!r}")
            # Re-assigning an existing key keeps its position, so a decision
            # does not reorder the log of what was asked.
            self._approvals[approval.approval_id] = approval

    async def load_approval(self, approval_id: ApprovalId) -> ApprovalRecord | None:
        with self._lock:
            return self._approvals.get(approval_id)

    async def list_approvals(
        self,
        *,
        instance_id: AgentInstanceId | None = None,
        agent_run_id: AgentRunId | None = None,
        status: ApprovalStatus | None = None,
    ) -> Sequence[ApprovalRecord]:
        with self._lock:
            approvals = list(self._approvals.values())
        if instance_id is not None:
            approvals = [a for a in approvals if a.instance_id == instance_id]
        if agent_run_id is not None:
            approvals = [a for a in approvals if a.agent_run_id == agent_run_id]
        if status is not None:
            approvals = [a for a in approvals if a.status is status]
        return tuple(approvals)  # oldest first

    async def record_session_grant(self, grant: SessionGrant) -> None:
        with self._lock:
            if grant.instance_id not in self._instances:
                raise KeyError(f"no agent instance {grant.instance_id!r}")
            # Re-assigning keeps the key's position, so revoking one does not
            # reorder the record of what was granted when.
            self._session_grants[grant.grant_id] = grant

    async def list_session_grants(
        self, *, instance_id: AgentInstanceId | None = None
    ) -> Sequence[SessionGrant]:
        with self._lock:
            grants = list(self._session_grants.values())
        if instance_id is not None:
            grants = [grant for grant in grants if grant.instance_id == instance_id]
        return tuple(grants)  # oldest first

    # --- beyond the port --------------------------------------------------

    async def agent_run(self, agent_run_id: AgentRunId) -> AgentRun | None:
        """Read back one execution. Not on the port -- nothing needs it there
        yet, and a port method with one in-memory implementation is a guess."""
        with self._lock:
            return self._agent_runs.get(agent_run_id)


__all__ = ["InMemoryStateStore"]

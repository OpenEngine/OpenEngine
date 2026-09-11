"""Pure domain vocabulary for the agent engine.

The innermost layer. Depends on nothing -- not the standard library's I/O, not
third-party packages, and certainly not adapters. Everything here is data.
"""

from engine.domain.agents import AgentInstance, AgentProfile, AgentRun, AgentRunStatus
from engine.domain.approvals import (
    ApprovalDecision,
    ApprovalDecisionSource,
    ApprovalKind,
    ApprovalRecord,
    ApprovalStatus,
    SessionGrant,
)
from engine.domain.chat import Conversation, Message, Role, ToolCall
from engine.domain.commands import (
    Command,
    Notify,
    PersistRun,
    ProvisionWorkspace,
    PublishChanges,
    ScheduleTimer,
    StartAgentRun,
)
from engine.domain.events import Event, RunFailed, StepCompleted
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
    WorkflowId,
    WorkstreamId,
    WorkOrderId,
    WorkspaceId,
)
from engine.domain.planning import (
    Milestone,
    Project,
    Workstream,
    instance_id_for_project,
    project_id_for_instance,
    workstreams_by_milestone,
)
from engine.domain.state import RunOrigin, RunPhase, RunState
from engine.domain.scoping import (
    MilestoneScope,
    ScopingPlan,
    ScopingPolicy,
    Supersession,
    WorkOrder,
    WorkOrderSpec,
    WorkOrderStatus,
)
from engine.domain.tools import ToolParameter, ToolParameterType, ToolSpec
from engine.domain.workflow import StepOutput, StepSpec

__all__ = [
    "AgentId",
    "AgentInstance",
    "AgentInstanceId",
    "AgentProfile",
    "AgentRun",
    "AgentRunId",
    "AgentRunStatus",
    "ApprovalDecision",
    "ApprovalDecisionSource",
    "ApprovalId",
    "ApprovalKind",
    "ApprovalRecord",
    "ApprovalStatus",
    "Command",
    "Conversation",
    "ConversationId",
    "Event",
    "Message",
    "MessageId",
    "Milestone",
    "MilestoneId",
    "MilestoneScope",
    "Notify",
    "PersistRun",
    "ProvisionWorkspace",
    "Project",
    "ProjectId",
    "instance_id_for_project",
    "project_id_for_instance",
    "PublishChanges",
    "Role",
    "RunFailed",
    "RunId",
    "RunOrigin",
    "RunPhase",
    "RunState",
    "ScheduleTimer",
    "ScopingPlan",
    "ScopingPolicy",
    "SessionGrant",
    "SessionGrantId",
    "StartAgentRun",
    "StepCompleted",
    "StepId",
    "StepOutput",
    "StepSpec",
    "TaskId",
    "ToolCall",
    "ToolParameter",
    "ToolParameterType",
    "ToolSpec",
    "Supersession",
    "WorkflowId",
    "Workstream",
    "WorkstreamId",
    "WorkOrder",
    "WorkOrderId",
    "WorkOrderSpec",
    "WorkOrderStatus",
    "workstreams_by_milestone",
    "WorkspaceId",
]

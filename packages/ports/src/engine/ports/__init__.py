"""The six capabilities the engine needs from the outside world.

Each port is a `Protocol` -- structural, so an adapter satisfies it by shape
alone with no import of this package required at runtime and no base class to
inherit. Ports name *what* is needed (publish changes, run an agent), never
*who* provides it (GitHub, Codex).

Every command in `engine.domain.commands` is fulfilled by exactly one of these.
"""

from engine.ports.agent_runner import (
    AgentRunner,
    AgentTurn,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalKind,
    ApprovalRequest,
    ApprovalResponse,
    FinishReason,
    InteractiveAgentRunner,
    InteractiveMcpAgentRunner,
    McpAgentRunner,
    McpServerConfig,
    StreamingAgentRunner,
    StreamingMcpAgentRunner,
    TokenUsage,
    TurnObserver,
    UserInputAnswer,
    UserInputOption,
    UserInputQuestion,
    UserInputResponse,
)
from engine.ports.communications import Communications
from engine.ports.permissions import (
    ApprovalCapability,
    PermissionScope,
    PermissionTranslator,
)
from engine.ports.source_control import SourceControl
from engine.ports.state_store import StateStore
from engine.ports.workflow_runtime import WorkflowRuntime
from engine.ports.workspace_provider import Workspace, WorkspaceProvider, WorkspaceState

__all__ = [
    "AgentRunner",
    "AgentTurn",
    "ApprovalDecision",
    "ApprovalHandler",
    "ApprovalKind",
    "ApprovalRequest",
    "ApprovalResponse",
    "ApprovalCapability",
    "Communications",
    "FinishReason",
    "InteractiveAgentRunner",
    "InteractiveMcpAgentRunner",
    "McpAgentRunner",
    "McpServerConfig",
    "PermissionScope",
    "PermissionTranslator",
    "SourceControl",
    "StateStore",
    "StreamingAgentRunner",
    "StreamingMcpAgentRunner",
    "TokenUsage",
    "TurnObserver",
    "UserInputAnswer",
    "UserInputOption",
    "UserInputQuestion",
    "UserInputResponse",
    "Workspace",
    "WorkflowRuntime",
    "WorkspaceProvider",
    "WorkspaceState",
]

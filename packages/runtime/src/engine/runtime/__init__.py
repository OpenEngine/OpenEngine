"""The runtime: binds the pure engine to concrete capabilities.

Depends on `engine.ports` (and through it `engine.domain`), never on a specific
adapter. Adapters depend on the runtime, not the other way around.
"""

from engine.runtime.approvals import (
    ApprovalBroker,
    ApprovalDecisionNotAllowedError,
    ApprovalError,
    ApprovalNotPendingError,
    ApprovalPresenter,
    ApprovalsUnsupportedError,
    UnknownApprovalError,
)
from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import Dispatcher, UnhandledCommandError
from engine.runtime.profiles import BUILT_IN, CODER, FOREMAN, UnknownAgentError, profile_for
from engine.runtime.run_read_model import RunReader, WorkflowRunView
from engine.runtime.session import (
    DEFAULT_RUNNER,
    INTERRUPTED_TOOL_RESULT,
    INTERRUPTED_TURN_NOTE,
    AgentSession,
    UnknownInstanceError,
    UnknownRunnerError,
    UnknownToolGrantError,
    WorkspacesUnavailableError,
)
from engine.runtime.session_grants import (
    matching_grant,
    normalized_scope,
    session_grant_from,
)
from engine.runtime.step_results import (
    INVALID_COMPLETION_ERROR,
    InvalidStepResultError,
    complete_step_tool,
    fail_step_tool,
    run_failed_from_tool_call,
    requests_clarification_or_escalation,
    step_completed_from_tool_call,
    run_failed_from_arguments,
    step_completed_from_arguments,
    step_result_instructions,
    step_result_from_tool_call,
)
from engine.runtime.terminal_mcp import (
    TerminalMcpBroker,
    TerminalResultAlreadySubmittedError,
    TerminalResultRegistry,
)
from engine.runtime.workflow_execution import WorkflowExecutionError, WorkflowExecutor

__all__ = [
    "INTERRUPTED_TOOL_RESULT",
    "INTERRUPTED_TURN_NOTE",
    "INVALID_COMPLETION_ERROR",
    "BUILT_IN",
    "CODER",
    "DEFAULT_RUNNER",
    "FOREMAN",
    "AgentSession",
    "ApprovalBroker",
    "ApprovalDecisionNotAllowedError",
    "ApprovalError",
    "ApprovalNotPendingError",
    "ApprovalPresenter",
    "ApprovalsUnsupportedError",
    "Capabilities",
    "Dispatcher",
    "InvalidStepResultError",
    "RunReader",
    "TerminalMcpBroker",
    "TerminalResultAlreadySubmittedError",
    "TerminalResultRegistry",
    "UnhandledCommandError",
    "UnknownAgentError",
    "UnknownApprovalError",
    "UnknownInstanceError",
    "UnknownRunnerError",
    "UnknownToolGrantError",
    "WorkspacesUnavailableError",
    "WorkflowRunView",
    "WorkflowExecutionError",
    "WorkflowExecutor",
    "complete_step_tool",
    "fail_step_tool",
    "matching_grant",
    "normalized_scope",
    "profile_for",
    "session_grant_from",
    "run_failed_from_tool_call",
    "requests_clarification_or_escalation",
    "step_completed_from_tool_call",
    "run_failed_from_arguments",
    "step_completed_from_arguments",
    "step_result_instructions",
    "step_result_from_tool_call",
]

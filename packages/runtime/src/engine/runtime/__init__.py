"""The runtime: binds the pure engine to concrete capabilities.

Depends on `engine.ports` (and through it `engine.domain`), never on a specific
adapter. Adapters depend on the runtime, not the other way around.
"""

from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import Dispatcher, UnhandledCommandError
from engine.runtime.profiles import BUILT_IN, CODER, FOREMAN, UnknownAgentError, profile_for
from engine.runtime.run_read_model import RunReader, WorkflowRunView
from engine.runtime.session import (
    DEFAULT_RUNNER,
    AgentSession,
    UnknownInstanceError,
    UnknownRunnerError,
    UnknownToolGrantError,
    WorkspacesUnavailableError,
)
from engine.runtime.step_results import (
    InvalidStepResultError,
    complete_step_tool,
    fail_step_tool,
    run_failed_from_tool_call,
    step_completed_from_tool_call,
    step_completed_from_turn,
    step_result_instructions,
    step_result_from_tool_call,
)
from engine.runtime.workflow_execution import WorkflowExecutionError, WorkflowExecutor

__all__ = [
    "BUILT_IN",
    "CODER",
    "DEFAULT_RUNNER",
    "FOREMAN",
    "AgentSession",
    "Capabilities",
    "Dispatcher",
    "InvalidStepResultError",
    "RunReader",
    "UnhandledCommandError",
    "UnknownAgentError",
    "UnknownInstanceError",
    "UnknownRunnerError",
    "UnknownToolGrantError",
    "WorkspacesUnavailableError",
    "WorkflowRunView",
    "WorkflowExecutionError",
    "WorkflowExecutor",
    "complete_step_tool",
    "fail_step_tool",
    "profile_for",
    "run_failed_from_tool_call",
    "step_completed_from_tool_call",
    "step_completed_from_turn",
    "step_result_instructions",
    "step_result_from_tool_call",
]

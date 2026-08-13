"""The runtime: binds the pure engine to concrete capabilities.

Depends on `engine.ports` (and through it `engine.domain`), never on a specific
adapter. Adapters depend on the runtime, not the other way around.
"""

from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import Dispatcher, UnhandledCommandError
from engine.runtime.profiles import BUILT_IN, CODER, FOREMAN, UnknownAgentError, profile_for
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
    step_completed_from_turn,
    step_result_instructions,
)

__all__ = [
    "BUILT_IN",
    "CODER",
    "DEFAULT_RUNNER",
    "FOREMAN",
    "AgentSession",
    "Capabilities",
    "Dispatcher",
    "InvalidStepResultError",
    "UnhandledCommandError",
    "UnknownAgentError",
    "UnknownInstanceError",
    "UnknownRunnerError",
    "UnknownToolGrantError",
    "WorkspacesUnavailableError",
    "profile_for",
    "step_completed_from_turn",
    "step_result_instructions",
]

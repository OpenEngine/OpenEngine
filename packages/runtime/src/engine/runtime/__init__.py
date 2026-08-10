"""The runtime: binds the pure engine to concrete capabilities.

Depends on `engine.ports` (and through it `engine.domain`), never on a specific
adapter. Adapters depend on the runtime, not the other way around.
"""

from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import Dispatcher, UnhandledCommandError
from engine.runtime.profiles import BUILT_IN, CODER, FOREMAN, UnknownAgentError, profile_for
from engine.runtime.session import AgentSession, UnknownInstanceError, UnknownToolGrantError

__all__ = [
    "BUILT_IN",
    "CODER",
    "FOREMAN",
    "AgentSession",
    "Capabilities",
    "Dispatcher",
    "UnhandledCommandError",
    "UnknownAgentError",
    "UnknownInstanceError",
    "UnknownToolGrantError",
    "profile_for",
]

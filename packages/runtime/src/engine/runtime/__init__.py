"""The runtime: binds the pure engine to concrete capabilities.

Depends on `engine.ports` (and through it `engine.domain`), never on a specific
adapter. Adapters depend on the runtime, not the other way around.
"""

from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import Dispatcher, UnhandledCommandError

__all__ = ["Capabilities", "Dispatcher", "UnhandledCommandError"]

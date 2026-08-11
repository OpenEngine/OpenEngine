"""Web control interface: an assistant-ui client over the engine.

A composition root. Depends on adapters; nothing depends on it.
"""

from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_runners,
    build_session,
)

__all__ = ["Settings", "build_capabilities", "build_runners", "build_session"]

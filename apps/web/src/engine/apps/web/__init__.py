"""Web control interface: a Streamlit view of the engine.

A composition root. Depends on adapters; nothing depends on it.
"""

from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_read_model,
    build_session,
)

__all__ = ["Settings", "build_capabilities", "build_read_model", "build_session"]

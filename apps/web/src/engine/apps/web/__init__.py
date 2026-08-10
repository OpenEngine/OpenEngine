"""Web control interface: a Streamlit view of the engine.

A composition root, and the third deployable alongside the control server and
the worker. Depends on adapters; nothing depends on it.

Unwired by design: the pages read from `readmodel`, whose only implementation
today is empty. See `app` for what is drawn and `composition` for the one
function that changes when it is connected.

The interface is also the intended first implementation of the Communications
capability -- the surface an agent's clarifying question surfaces on, and where
the answer re-enters the workflow as an event. That lands with the
communications ticket; the Inbox page is its placeholder.
"""

from engine.apps.web.composition import Settings, build_capabilities, build_read_model

__all__ = ["Settings", "build_capabilities", "build_read_model"]

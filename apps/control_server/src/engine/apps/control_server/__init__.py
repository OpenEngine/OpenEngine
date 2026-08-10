"""Control server: serves the planner surface.

A composition root, but a replaceable one. It names no adapter -- the backend is
resolved by name from installed plugins, so a consumer swaps providers with
configuration rather than a fork. The surface itself lives in `engine.web`.
"""

from engine.apps.control_server.composition import (
    DEFAULT_RUNNER_PREFERENCE,
    Settings,
    build_agent_runner,
)

__all__ = ["DEFAULT_RUNNER_PREFERENCE", "Settings", "build_agent_runner"]

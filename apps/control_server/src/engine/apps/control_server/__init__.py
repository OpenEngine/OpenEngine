"""Control server: accepts run requests and starts durable runs.

A composition root. Depends on adapters; nothing depends on it.
"""

from engine.apps.control_server.composition import Settings, build_capabilities

__all__ = ["Settings", "build_capabilities"]

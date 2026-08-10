"""Worker: executes engine commands against real capabilities.

A composition root. Depends on adapters; nothing depends on it.
"""

from engine.apps.worker.composition import Settings, build_capabilities, build_dispatcher

__all__ = ["Settings", "build_capabilities", "build_dispatcher"]

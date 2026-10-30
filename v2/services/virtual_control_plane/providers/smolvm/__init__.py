"""Persistent, owned smolvm microVMs as managed workspaces."""

from virtual_control_plane.providers.smolvm.provider import (
    DEFAULT_MAX_RESOURCES, DEFAULT_TOOLS, GUEST_ROOT, Smolvm, machine_name,
)

__all__ = ["DEFAULT_MAX_RESOURCES", "DEFAULT_TOOLS", "GUEST_ROOT", "Smolvm", "machine_name"]

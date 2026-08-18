"""Provider-neutral approval settings and the runner translation boundary.

Engine configuration names capabilities such as ``read`` and ``bash``. A
runner names whatever its provider emits. The translator is the deliberately
small seam between them: it classifies one provider approval request without
deciding whether Engine should allow it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from engine.ports.agent_runner import ApprovalRequest


class ApprovalCapability(StrEnum):
    """Engine's provider-neutral approval vocabulary."""

    BASH = "bash"
    EDIT = "edit"
    MCP = "mcp"
    READ = "read"
    WEB = "web"


@dataclass(frozen=True, slots=True)
class PermissionScope:
    """The Engine capability and optional value one request addresses."""

    capability: ApprovalCapability
    value: str | None = None


@runtime_checkable
class PermissionTranslator(Protocol):
    """Classifies approval requests emitted by one runner's provider."""

    def scope_for(self, request: ApprovalRequest) -> PermissionScope | None:
        """Return the Engine scope, or ``None`` when the request is unknown.

        Unknown requests deliberately remain unclassified so policy evaluation
        can fail closed and put them to a person.
        """
        ...


__all__ = [
    "ApprovalCapability",
    "PermissionScope",
    "PermissionTranslator",
]

"""Translate Codex app-server approval requests into Engine permissions.

One direction only, unlike the Claude adapter's. Codex's pre-turn knob is its
sandbox, and a sandbox is a ceiling rather than a preapproval: a capability
absent from `approvals.allow` is one nobody has ruled on, so a person may still
approve it mid-turn -- and an OS-level boundary narrowed before the turn started
would then refuse what they just allowed. So the policy reaches Codex through
the approval callback and nowhere else, and the sandbox stays wide enough to
honour whatever comes back through it.
"""

from engine.domain.approvals import ApprovalKind
from engine.ports.agent_runner import ApprovalRequest
from engine.ports.permissions import ApprovalCapability, PermissionScope


class CodexPermissionTranslator:
    """Classify the two approval request kinds Codex app-server exposes."""

    def scope_for(self, request: ApprovalRequest) -> PermissionScope | None:
        if request.kind is ApprovalKind.COMMAND_EXECUTION:
            return PermissionScope(ApprovalCapability.BASH, request.command)
        if request.kind is ApprovalKind.FILE_CHANGE:
            return PermissionScope(ApprovalCapability.EDIT)
        return None


CODEX_PERMISSION_TRANSLATOR = CodexPermissionTranslator()


__all__ = ["CODEX_PERMISSION_TRANSLATOR", "CodexPermissionTranslator"]

"""Translate Codex app-server approval requests into Engine permissions."""

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

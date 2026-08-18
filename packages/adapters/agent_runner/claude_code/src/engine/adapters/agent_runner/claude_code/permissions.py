"""Translate Claude Code tool requests into Engine permissions."""

from engine.domain.approvals import ApprovalKind
from engine.ports.agent_runner import ApprovalRequest
from engine.ports.permissions import ApprovalCapability, PermissionScope

READ_TOOLS = frozenset({"Read", "Glob", "Grep"})
EDIT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit"})
WEB_TOOLS = frozenset({"WebFetch", "WebSearch"})


class ClaudePermissionTranslator:
    """Classify Claude's provider tool names without exposing them to config."""

    def scope_for(self, request: ApprovalRequest) -> PermissionScope | None:
        tool_name = request.tool_name or ""
        if request.kind is ApprovalKind.COMMAND_EXECUTION or tool_name == "Bash":
            return PermissionScope(ApprovalCapability.BASH, request.command)
        if request.kind is ApprovalKind.FILE_CHANGE or tool_name in EDIT_TOOLS:
            return PermissionScope(ApprovalCapability.EDIT)
        if tool_name in READ_TOOLS:
            return PermissionScope(ApprovalCapability.READ)
        if tool_name in WEB_TOOLS:
            return PermissionScope(ApprovalCapability.WEB)
        if tool_name.startswith("mcp__"):
            return PermissionScope(ApprovalCapability.MCP)
        return None


CLAUDE_PERMISSION_TRANSLATOR = ClaudePermissionTranslator()


__all__ = [
    "CLAUDE_PERMISSION_TRANSLATOR",
    "ClaudePermissionTranslator",
    "EDIT_TOOLS",
    "READ_TOOLS",
    "WEB_TOOLS",
]

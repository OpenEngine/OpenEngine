"""Translate ACP permission requests into Engine permissions.

One translator for every ACP agent, because the request is ACP's rather than
the agent's: a `toolCall` with a `kind`, and -- from Claude's adapter -- the
Claude tool name under `_meta.claudeCode.toolName`. `ACPAgentRunner` reads both
into `ApprovalRequest.tool_name`, preferring the tool name, so this classifies
whichever arrived.
"""

from engine.domain.approvals import ApprovalKind
from engine.ports.agent_runner import ApprovalRequest
from engine.ports.permissions import ApprovalCapability, PermissionScope

#: Claude's own tool names and ACP's tool kinds, by the capability that covers
#: them. The shell and file-change kinds are classified by `ApprovalKind`.
READ_TOOLS = frozenset({"Read", "Glob", "Grep", "read", "search"})
WEB_TOOLS = frozenset({"WebFetch", "WebSearch", "fetch"})


class ACPPermissionTranslator:
    """Classify an ACP agent's permission request as an Engine capability."""

    def scope_for(self, request: ApprovalRequest) -> PermissionScope | None:
        tool_name = request.tool_name or ""
        if request.kind is ApprovalKind.COMMAND_EXECUTION:
            return PermissionScope(ApprovalCapability.BASH, request.command)
        if request.kind is ApprovalKind.FILE_CHANGE:
            return PermissionScope(ApprovalCapability.EDIT)
        if tool_name in READ_TOOLS:
            return PermissionScope(ApprovalCapability.READ)
        if tool_name in WEB_TOOLS:
            return PermissionScope(ApprovalCapability.WEB)
        if tool_name.startswith("mcp__"):
            return PermissionScope(ApprovalCapability.MCP)
        return None


ACP_PERMISSION_TRANSLATOR = ACPPermissionTranslator()


__all__ = [
    "ACP_PERMISSION_TRANSLATOR",
    "ACPPermissionTranslator",
    "READ_TOOLS",
    "WEB_TOOLS",
]

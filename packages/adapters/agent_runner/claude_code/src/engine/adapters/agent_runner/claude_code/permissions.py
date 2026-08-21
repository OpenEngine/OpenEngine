"""Translate between Claude Code tool requests and Engine permissions.

Both directions, because a policy has to reach the provider before the turn
starts as well as after it pauses. `scope_for` reads one request Claude has
already made; `allowed_tools_for` says which of Claude's tools a capability set
preapproves, so the ones a policy grants never become a request at all.
"""

from collections.abc import Iterable

from engine.domain.approvals import ApprovalKind
from engine.ports.agent_runner import ApprovalRequest
from engine.ports.permissions import ApprovalCapability, PermissionScope

#: Claude's own tools, grouped by the Engine capability that covers them. One
#: table rather than two, so a tool cannot be classified as one thing on the way
#: in and preapproved as another on the way out.
CAPABILITY_TOOLS: dict[ApprovalCapability, tuple[str, ...]] = {
    ApprovalCapability.READ: ("Read", "Glob", "Grep"),
    ApprovalCapability.EDIT: ("Edit", "Write", "NotebookEdit"),
    ApprovalCapability.WEB: ("WebFetch", "WebSearch"),
}

READ_TOOLS = frozenset(CAPABILITY_TOOLS[ApprovalCapability.READ])
EDIT_TOOLS = frozenset(CAPABILITY_TOOLS[ApprovalCapability.EDIT])
WEB_TOOLS = frozenset(CAPABILITY_TOOLS[ApprovalCapability.WEB])


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


def allowed_tools_for(
    capabilities: Iterable[ApprovalCapability],
) -> tuple[str, ...]:
    """The `--allowedTools` list a capability set preapproves.

    Only the capabilities whose whole answer is a tool name, which is why
    `bash` and `mcp` are absent and stay absent:

    * A shell policy is written per command. A preapproved `Bash` would run the
      commands `approvals.bash.deny` names before anything could consult it, so
      shell always reaches the approval callback -- which is where the patterns
      are evaluated.
    * An MCP grant names a server, and this list is built before any server is
      bound.

    Neither is thereby refused. Both are decided one request at a time by the
    policy, rather than wholesale before the turn starts.
    """
    granted = set(capabilities)
    return tuple(
        tool
        for capability, tools in CAPABILITY_TOOLS.items()
        if capability in granted
        for tool in tools
    )


CLAUDE_PERMISSION_TRANSLATOR = ClaudePermissionTranslator()


__all__ = [
    "CAPABILITY_TOOLS",
    "CLAUDE_PERMISSION_TRANSLATOR",
    "ClaudePermissionTranslator",
    "EDIT_TOOLS",
    "READ_TOOLS",
    "WEB_TOOLS",
    "allowed_tools_for",
]

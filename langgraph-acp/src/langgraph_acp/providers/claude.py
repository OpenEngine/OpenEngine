"""Claude Code, reached through its ACP adapter."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from langgraph_acp.agent import StdioACPProvider, launch_command
from langgraph_acp.client import ACPClient
from langgraph_acp.elicitation import ACPElicitationHandler
from langgraph_acp.permissions import ACPPermissionHandler

# Upgrade deliberately and pass the adapter contract check in test_adapter_compatibility.py.
CLAUDE_ACP_VERSION = "0.81.0"
CLAUDE_ACP_COMMAND = (
    "npx",
    "--yes",
    f"@agentclientprotocol/claude-agent-acp@{CLAUDE_ACP_VERSION}",
)


@dataclass(frozen=True, slots=True)
class ClaudeACPProvider:
    """Reach Claude Code through the maintained Claude ACP adapter."""

    name: str = "claude"
    command: Sequence[str] = CLAUDE_ACP_COMMAND
    env: Mapping[str, str] | None = None
    cwd: str | os.PathLike[str] | None = None
    permissions: ACPPermissionHandler | None = None
    """Who answers `session/request_permission`. `None` declines every request."""
    elicitations: ACPElicitationHandler | None = None
    """Who answers `elicitation/create`. `None` does not offer to."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", launch_command(self.command))

    async def connect(self) -> ACPClient:
        return await StdioACPProvider(
            name=self.name,
            command=self.command,
            env=self.env,
            cwd=self.cwd,
            permissions=self.permissions,
            elicitations=self.elicitations,
        ).connect()


__all__ = ["CLAUDE_ACP_COMMAND", "ClaudeACPProvider"]

"""Claude Code, reached through its ACP adapter."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from langgraph_acp.agent import StdioACPProvider, launch_command
from langgraph_acp.client import ACPClient

CLAUDE_ACP_COMMAND = ("npx", "--yes", "@zed-industries/claude-agent-acp")


@dataclass(frozen=True, slots=True)
class ClaudeACPProvider:
    """Reach Claude Code through the maintained Claude ACP adapter."""

    name: str = "claude"
    command: Sequence[str] = CLAUDE_ACP_COMMAND
    env: Mapping[str, str] | None = None
    cwd: str | os.PathLike[str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", launch_command(self.command))

    async def connect(self) -> ACPClient:
        return await StdioACPProvider(
            name=self.name, command=self.command, env=self.env, cwd=self.cwd
        ).connect()


__all__ = ["CLAUDE_ACP_COMMAND", "ClaudeACPProvider"]

"""OpenCode, which speaks ACP itself.

No adapter sits in between: `opencode acp` is the agent. Running the pinned
`opencode-ai` package through `npx` is what makes `ACPNode(agent="opencode")`
work without a global install, and an installation with its own OpenCode
overrides the command and keeps everything else:

    OpenCodeACPProvider(command=["opencode", "acp"])

Authentication and models are OpenCode's own: it reads the operator's
`opencode.json` and whatever `opencode auth login` left behind, and nothing here
reads or carries a credential. `OPENCODE_CONFIG_CONTENT` is the configuration
OpenCode merges over those files, so `env` is how a caller changes what a
session may do without asking:

    OpenCodeACPProvider(env={"OPENCODE_CONFIG_CONTENT": '{"permission": {"bash": "ask"}}'})
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from langgraph_acp.agent import StdioACPProvider, launch_command
from langgraph_acp.client import ACPClient
from langgraph_acp.elicitation import ACPElicitationHandler
from langgraph_acp.permissions import ACPPermissionHandler

# Upgrade deliberately and pass the adapter contract check in test_adapter_compatibility.py.
OPENCODE_VERSION = "1.18.32"

# Windows CreateProcess needs the npm command shim extension.
OPENCODE_ACP_COMMAND = (
    "npx.cmd" if os.name == "nt" else "npx",
    "--yes",
    f"opencode-ai@{OPENCODE_VERSION}",
    "acp",
)


@dataclass(frozen=True, slots=True)
class OpenCodeACPProvider:
    """Reach OpenCode through its built-in ACP server.

    Registered as `"opencode"` by default, which is the name a graph writes:

        ACPNode(agent="opencode")
    """

    name: str = "opencode"
    command: Sequence[str] = OPENCODE_ACP_COMMAND
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


__all__ = ["OPENCODE_ACP_COMMAND", "OpenCodeACPProvider"]

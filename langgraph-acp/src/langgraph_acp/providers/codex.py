"""Codex, reached over ACP.

The Codex CLI does not speak ACP itself; `@agentclientprotocol/codex-acp` is the
adapter that wraps it, and running it through `npx` is what makes
`ACPNode(agent="codex")` work on a machine where only Codex is installed.

An installation that would rather not shell out to `npx` -- a container image
with the adapter baked in, an air-gapped runner -- overrides the command and
keeps everything else:

    CodexACPProvider(command=["codex-acp"])

Authentication is Codex's own: the adapter uses whatever `codex login` left
behind. Nothing here reads or carries a credential, which is the property the
secrets ticket has to preserve rather than establish.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from langgraph_acp.agent import StdioACPProvider, launch_command
from langgraph_acp.client import ACPClient
from langgraph_acp.permissions import ACPPermissionHandler

#: The ACP adapter for Codex, run without a global install. Pinned to no version
#: on purpose: the adapter tracks the Codex CLI, and an old pin here would fail
#: against a current Codex rather than protect anyone from it.
CODEX_ACP_COMMAND = ("npx", "--yes", "@agentclientprotocol/codex-acp")


@dataclass(frozen=True, slots=True)
class CodexACPProvider:
    """Reach Codex through its ACP adapter.

    Registered as `"codex"` by default, which is the name a graph writes:

        ACPNode(agent="codex")
    """

    name: str = "codex"
    """Change it to register the same agent twice under different settings."""
    command: Sequence[str] = CODEX_ACP_COMMAND
    """The ACP adapter to launch. Override to use a locally installed one."""
    env: Mapping[str, str] | None = None
    """Overlaid on this process's environment, never replacing it."""
    cwd: str | os.PathLike[str] | None = None
    """Where to launch the adapter. Not the workspace a session is given."""
    permissions: ACPPermissionHandler | None = None
    """Who answers `session/request_permission`. `None` declines every request."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", launch_command(self.command))

    async def connect(self) -> ACPClient:
        return await StdioACPProvider(
            name=self.name,
            command=self.command,
            env=self.env,
            cwd=self.cwd,
            permissions=self.permissions,
        ).connect()


__all__ = ["CODEX_ACP_COMMAND", "CodexACPProvider"]

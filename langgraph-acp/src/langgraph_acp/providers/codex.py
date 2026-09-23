"""Codex, reached over ACP.

The Codex CLI does not speak ACP itself; `@agentclientprotocol/codex-acp` is the
adapter that does, and running it through `npx` is what makes
`ACPNode(agent="codex")` work without a global install of anything.

**The adapter brings its own Codex.** It depends on `@openai/codex` and drives
that as `codex app-server`, so the `codex` on the operator's `PATH` is not what
answers here -- adapter 1.9.0 ships Codex 0.153.2 whatever is installed. Two
consequences worth knowing before reading a surprising transcript:

* This path runs a Codex outside the release matrix in
  `.github/cli-versions.json`, which pins what the step-workflow CLIs are tested
  against. The two paths reach different Codex versions by construction.
* `CODEX_PATH` is how the adapter is told to run a specific binary instead, and
  it needs no support from this package -- `env` reaches it:

      CodexACPProvider(env={"CODEX_PATH": "/usr/local/bin/codex"})

An installation that would rather not shell out to `npx` -- a container image
with the adapter baked in, an air-gapped runner -- overrides the command and
keeps everything else:

    CodexACPProvider(command=["codex-acp"])

Authentication is Codex's own, and is unaffected by which binary runs: the
bundled Codex reads `CODEX_HOME` (`~/.codex` by default), so it uses whatever
`codex login` left behind. Nothing here reads or carries a credential, which is
the property the secrets ticket has to preserve rather than establish.
"""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from langgraph_acp.agent import StdioACPProvider, launch_command
from langgraph_acp.client import ACPClient
from langgraph_acp.elicitation import ACPElicitationHandler
from langgraph_acp.permissions import ACPPermissionHandler

# Upgrade deliberately and pass the adapter contract check in test_adapter_compatibility.py.
CODEX_ACP_VERSION = "1.13.0"

# Windows CreateProcess needs the npm command shim extension.
CODEX_ACP_COMMAND = (
    "npx.cmd" if os.name == "nt" else "npx",
    "--yes",
    f"@agentclientprotocol/codex-acp@{CODEX_ACP_VERSION}",
)


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


__all__ = ["CODEX_ACP_COMMAND", "CodexACPProvider"]

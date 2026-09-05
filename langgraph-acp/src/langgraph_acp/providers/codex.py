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
from langgraph_acp.permissions import ACPPermissionHandler

#: The ACP adapter for Codex, run without a global install.
#:
#: Unpinned, which is a deliberate exemption from the policy `cli-versions.json`
#: states for the provider CLIs, not an oversight. That policy pins so a red
#: compatibility run names a version somebody can reinstall; the cost of a pin
#: here is different, because the adapter ships the Codex it drives. Pinning
#: would freeze both halves against a service that keeps moving -- model
#: retirements and auth changes arrive from the far side, where no pin helps --
#: and this repository has no `cli-versions.yml` equivalent that would notice
#: the pin going stale.
#:
#: What that exemption costs: a new major lands in production without a diff.
#: What limits it: the `agents over ACP` job in `cli-compatibility.yml` runs the
#: adapter on a schedule and records the version `npx` resolved, so a break
#: names a version even though this line does not.
#:
#: An installation that wants the pin takes it, and gives up the above:
#: `CodexACPProvider(command=["npx", "--yes", "@agentclientprotocol/codex-acp@1.9.0"])`.
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

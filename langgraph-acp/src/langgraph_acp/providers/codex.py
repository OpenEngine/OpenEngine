"""Codex, reached over ACP.

The Codex CLI does not speak ACP itself; `@agentclientprotocol/codex-acp` is the
adapter that does, and running it through `npx` is what makes
`ACPNode(agent="codex")` work without a global install of anything.

The provider prefers the installed `codex` on the effective PATH, so discovery
and execution use the same version and account configuration. It sets the
adapter's `CODEX_PATH` to that executable. An explicit `CODEX_PATH` in the
provider environment or host environment takes precedence. If no installed
Codex is found, the pinned adapter's bundled `@openai/codex` is the fallback.

    CodexACPProvider(env={"CODEX_PATH": "/usr/local/bin/codex"})

An installation that would rather not shell out to `npx` -- a container image
with the adapter baked in, an air-gapped runner -- overrides the command and
keeps everything else:

    CodexACPProvider(command=["codex-acp"])

Authentication is Codex's own, and is unaffected by which binary runs: the
bundled Codex reads `CODEX_HOME` (`~/.codex` by default), so it uses whatever
`codex login` left behind. Nothing here reads or carries a credential, which is
the property the secrets ticket has to preserve rather than establish.

An `OPENAI_API_KEY` in the environment is not a login: the adapter reads it only
when asked to authenticate with its `api-key` method, and this package never
asks. Without a login, `session/new` is refused as "Authentication required".
The adapter's `DEFAULT_AUTH_REQUEST` makes it authenticate on demand instead:

    CodexACPProvider(env={"DEFAULT_AUTH_REQUEST": '{"methodId": "api-key"}'})
"""

import os
import shutil
import asyncio
import re
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

    def launch_environment(self) -> dict[str, str]:
        """Use the same Codex installation that backend discovery checks.

        An explicit CODEX_PATH wins. Keep the adapter's bundled binary as a
        fallback for installations which only installed the ACP adapter.
        """
        env = dict(self.env or {})
        if not env.get("CODEX_PATH") and not os.environ.get("CODEX_PATH"):
            binary = shutil.which("codex", path=env.get("PATH", os.environ.get("PATH")))
            if binary:
                env["CODEX_PATH"] = os.path.abspath(binary)
        return env

    async def runtime_description(self) -> str:
        """Identify the effective binary in startup errors without reading auth."""
        env = {**os.environ, **self.launch_environment()}
        binary = env.get("CODEX_PATH")
        if not binary:
            return f"codex-acp {CODEX_ACP_VERSION}, adapter-bundled Codex"
        process = None
        try:
            async with asyncio.timeout(3):
                process = await asyncio.create_subprocess_exec(
                    binary, "--version", stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=env,
                )
                output, _ = await process.communicate()
                version = re.search(rb"codex-cli [0-9][0-9A-Za-z.+-]*", output)
                if version:
                    return f"{binary} ({version[0].decode()})"
        except (OSError, TimeoutError):
            pass
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
        return binary

    async def connect(self) -> ACPClient:
        return await StdioACPProvider(
            name=self.name,
            command=self.command,
            env=self.launch_environment(),
            cwd=self.cwd,
            permissions=self.permissions,
            elicitations=self.elicitations,
        ).connect()


__all__ = ["CODEX_ACP_COMMAND", "CodexACPProvider"]

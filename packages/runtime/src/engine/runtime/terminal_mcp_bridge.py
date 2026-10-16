"""A run-bound MCP capability. The guest receives no host executable or forge identity."""

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any
from engine.ports.sandbox_home import SandboxHome
from engine.ports.sandbox_socket import SandboxSocket

BOOTSTRAP = "from pathlib import Path; import runpy; runpy.run_path(str(Path.home()/'.oe-mcp'/'shim.py'), run_name='__main__')"


@dataclass(frozen=True, slots=True)
class TerminalMcpBridge:
    name: str
    socket: SandboxSocket
    token: str = field(repr=False)
    identity: str = "terminal"

    @property
    def config(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": "python3",
            "args": ["-c", BOOTSTRAP, self.identity + ".json"],
            "env": [],
        }

    def home(self) -> SandboxHome:
        return SandboxHome(
            files={
                ".oe-mcp/shim.py": Path(__file__)
                .with_name("terminal_mcp_shim.py")
                .read_text(),
                f".oe-mcp/{self.identity}.json": json.dumps(
                    {"socket": self.socket.guest, "token": self.token}
                ),
            },
            redactions=(self.token,),
        )

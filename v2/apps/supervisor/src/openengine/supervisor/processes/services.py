"""Start the services, each as `python -m <module>` with the supervisor's interpreter."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import IO

# Start order; the supervisor stops them in reverse. vcp is last, so it starts
# after NATS and every other service, and stops before all of them.
SERVICES = (
    ("cxn", "connection_bus"),
    ("ge", "graph_engine"),
    ("tools", "tool_service"),
    ("vcp", "virtual_control_plane"),
)
# The services the supervisor waits for: each writes its health URL to this variable's file once ready.
READY_FILE_VARIABLES = {"ge": "OE_GE_READY_FILE", "vcp": "OE_VCP_READY_FILE"}
# Workspace-owning services that need an exclusive directory of their own; each
# writes its output and worktrees under the path this variable names.
STATE_DIR_VARIABLES = {"vcp": "OE_VCP_STATE_DIR"}
NATS_URL_VARIABLE = "OE_NATS_URL"


def start(module: str, log: IO[bytes], *, nats_url: str, environment: dict[str, str] | None = None) -> subprocess.Popen:
    """Start one service, its output going to `log`; it inherits the environment, telemetry settings included."""
    return subprocess.Popen(
        [sys.executable, "-m", module],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env={**os.environ, NATS_URL_VARIABLE: nats_url, **(environment or {})},
    )


def ready_file(state_directory: Path, name: str) -> Path:
    return state_directory / f"{name}.ready"

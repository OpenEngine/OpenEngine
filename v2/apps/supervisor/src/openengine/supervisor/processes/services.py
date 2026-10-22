"""Start the services, each as `python -m <module>` with the supervisor's interpreter."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import IO

# Start order; the supervisor stops them in reverse.
SERVICES = (
    ("cxn", "connection_bus"),
    ("ge", "graph_engine"),
    ("tools", "tool_service"),
    ("vcp", "virtual_control_plane"),
)
NATS_URL_VARIABLE = "OE_NATS_URL"


def start(module: str, log: IO[bytes], *, nats_url: str) -> subprocess.Popen:
    """Start one service, its output going to `log`; it inherits the environment, telemetry settings included."""
    return subprocess.Popen(
        [sys.executable, "-m", module],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env={**os.environ, NATS_URL_VARIABLE: nats_url},
    )

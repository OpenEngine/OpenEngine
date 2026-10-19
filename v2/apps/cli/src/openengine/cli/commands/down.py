"""`oe down`: stop the supervisor, which stops everything it started."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from openengine.cli.commands.status import running_state
from openengine.supervisor.state import is_alive

# The supervisor gives each child up to 10s to stop cleanly.
STOP_TIMEOUT_SECONDS = 30.0
POLL_SECONDS = 0.1
EXIT_OK = 0
EXIT_FAILED = 1


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("down", help="stop OpenEngine locally")
    parser.set_defaults(command=run)


def run(arguments: argparse.Namespace) -> int:
    state = running_state()
    if state is None:
        print("OpenEngine is not running.")
        return EXIT_OK
    os.kill(state.pid, signal.SIGTERM)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not is_alive(state.pid):
            print("OpenEngine stopped.")
            return EXIT_OK
        time.sleep(POLL_SECONDS)
    print(f"oe down: the supervisor (pid {state.pid}) did not stop within {STOP_TIMEOUT_SECONDS:.0f}s", file=sys.stderr)
    return EXIT_FAILED

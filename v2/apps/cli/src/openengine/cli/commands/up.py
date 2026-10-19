"""`oe up`: start the supervisor in the background and wait until it is ready."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from openengine.cli.commands.status import render, running_state
from openengine.cli.config.local import (
    HOST,
    NATS_MONITOR_PORT,
    NATS_PORT,
    cache_directory,
    state_directory,
    supervisor_log,
)
from openengine.cli.output.banner import banner
from openengine.supervisor.state import Status, read_state, state_path

# Long enough for the first run to download nats-server.
READY_TIMEOUT_SECONDS = 180.0
POLL_SECONDS = 0.1
EXIT_OK = 0
EXIT_FAILED = 1


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("up", help="start OpenEngine locally in the background")
    parser.add_argument("--nats-port", type=int, default=NATS_PORT, help="NATS client port")
    parser.add_argument("--nats-monitor-port", type=int, default=NATS_MONITOR_PORT, help="NATS monitoring port")
    parser.add_argument("--nats-server", type=Path, help="use this nats-server binary instead of the pinned download")
    parser.set_defaults(command=run)


def supervisor_command(arguments: argparse.Namespace) -> list[str]:
    command = [
        sys.executable, "-m", "openengine.supervisor",
        "--state-dir", str(state_directory()),
        "--cache-dir", str(cache_directory()),
        "--host", HOST,
        "--nats-port", str(arguments.nats_port),
        "--nats-monitor-port", str(arguments.nats_monitor_port),
    ]
    if arguments.nats_server:
        command += ["--nats-server", str(arguments.nats_server.absolute())]
    return command


def _fail(message: str) -> int:
    print(f"oe up: {message}\nSee {supervisor_log()}", file=sys.stderr)
    return EXIT_FAILED


def run(arguments: argparse.Namespace) -> int:
    if state := running_state():
        print(f"OpenEngine is already running.\n{render(state)}")
        return EXIT_OK
    print(banner(), end="\n\n", flush=True)
    log = supervisor_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    path = state_path(state_directory())
    with log.open("ab") as output:
        # A new session, so the supervisor outlives this command and the
        # terminal's Ctrl-C does not reach it.
        supervisor = subprocess.Popen(
            supervisor_command(arguments),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            state = read_state(path)
            if state and state.pid == supervisor.pid:
                if state.status == Status.READY:
                    print(render(state))
                    return EXIT_OK
                if state.status == Status.FAILED:
                    supervisor.wait()
                    return _fail(state.error or "the supervisor failed to start")
            if (code := supervisor.poll()) is not None:
                return _fail(f"the supervisor exited with status {code} before it was ready")
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        supervisor.terminate()
        supervisor.wait()
        return _fail("interrupted; the supervisor was stopped")
    supervisor.terminate()
    supervisor.wait()
    return _fail(f"the supervisor was not ready within {READY_TIMEOUT_SECONDS:.0f}s and was stopped")

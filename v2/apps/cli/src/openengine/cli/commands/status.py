"""`oe status`: what the supervisor and its processes are doing."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from urllib.error import URLError
from urllib.request import urlopen

from openengine.cli.config.local import state_directory
from openengine.cli.output.render import as_json, table
from openengine.supervisor.state import Status, SupervisorState, is_alive, read_state, state_path

EXIT_RUNNING = 0
EXIT_NOT_RUNNING = 1


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("status", help="show whether OpenEngine is running locally")
    parser.add_argument("--json", action="store_true", help="print the supervisor state as JSON")
    parser.set_defaults(command=run)


def running_state() -> SupervisorState | None:
    """The recorded state, if the supervisor that wrote it is still alive."""
    state = read_state(state_path(state_directory()))
    return state if state and is_alive(state.pid) else None


def render(state: SupervisorState) -> str:
    rows = [["supervisor", str(state.pid), state.status, ""]]
    rows += [[process.name, str(process.pid), process.status, process.address] for process in state.processes]
    return table(["NAME", "PID", "STATUS", "ADDRESS"], rows)


def run(arguments: argparse.Namespace) -> int:
    state = running_state()
    if state and state.status == Status.READY:
        processes = []
        for process in state.processes:
            if process.address.startswith("http://"):
                try:
                    with urlopen(process.address, timeout=0.5) as response:
                        healthy = response.status == 200
                except (URLError, OSError, ValueError):
                    healthy = False
                process = replace(process, status=Status.READY if healthy and is_alive(process.pid) else Status.UNREADY)
            elif not is_alive(process.pid):
                process = replace(process, status=Status.UNREADY)
            processes.append(process)
        state = replace(state, processes=processes, status=(
            Status.UNREADY if any(p.status == Status.UNREADY for p in processes) else Status.READY
        ))
    if arguments.json:
        print(as_json(asdict(state) if state else None))
    else:
        print(render(state) if state else "OpenEngine is not running. Start it with `oe up`.")
    return EXIT_RUNNING if state and state.status == Status.READY else EXIT_NOT_RUNNING

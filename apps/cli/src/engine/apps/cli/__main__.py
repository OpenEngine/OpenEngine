"""Run the OpenEngine daemon and drive its graphs, runs and loops from a terminal."""

from __future__ import annotations

import argparse
from importlib.metadata import version

from engine.apps.cli import connect, daemon
from engine.cli import commands as graph_commands

EXIT_OK = 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="engine", description=__doc__)
    result.add_argument("--version", action="version", version=version("engine-cli"))
    commands = result.add_subparsers(dest="command")
    connect.add_parser(commands)
    daemon.add_parser(commands)
    doctor = commands.add_parser("doctor", help="check local configuration, tools and SmolVM support")
    doctor.add_argument("--json", action="store_true")
    graph_commands.add_parsers(commands)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command is None:
        parser().print_help()
        return EXIT_OK
    if arguments.command == "connect":
        return connect.main(arguments)
    if arguments.command == "daemon":
        return daemon.main(arguments)
    if arguments.command == "doctor":
        return daemon.command_doctor(arguments)
    if arguments.command in graph_commands.COMMANDS:
        return graph_commands.main(arguments)
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())

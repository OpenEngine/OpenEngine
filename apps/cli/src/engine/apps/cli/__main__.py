'''Start a change with engine go "INSTRUCTION", or manage graphs, runs and backends.'''

from __future__ import annotations

import argparse
from importlib.metadata import version

from engine.apps.cli import connect, daemon, session, vm
from engine.cli import commands as graph_commands

EXIT_OK = 0
OUTPUT = """\
output:
  List operations print a table; add --json for JSON,
  such as `engine run list --json`.
  engine go follows live on a TTY; use --wait --json for scripts.
  Other commands print readable lines; add --json for JSON.
"""


class AllCommandsHelp(argparse.Action):
    def __call__(self, argument_parser, namespace, values, option_string=None):
        parser(all_commands=True).print_help()
        argument_parser.exit()


def parser(*, all_commands: bool = False) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="engine", description=__doc__, epilog=OUTPUT, formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    result.add_argument("-h", "--help", "--all", action=AllCommandsHelp, nargs=0, help="show all commands and exit")
    result.add_argument("--version", action="version", version=version("engine-cli"))
    commands = result.add_subparsers(dest="command")
    session.add_parser(graph_commands.add_parsers(commands))
    connect.add_parsers(commands)
    daemon.add_parser(commands)
    vm.add_parser(commands)
    doctor = commands.add_parser("doctor", help="check local configuration, tools and SmolVM support")
    doctor.add_argument("--json", action="store_true")
    if not all_commands:
        commands._choices_actions[:] = [action for action in commands._choices_actions if action.dest not in graph_commands.HIDDEN]
        commands.metavar = "{" + ",".join(name for name in commands.choices if name not in graph_commands.HIDDEN) + "}"
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command is None:
        parser().print_help()
        return EXIT_OK
    if arguments.command == "connect":
        return connect.main(arguments)
    if arguments.command == "connection":
        return connect.connections(arguments)
    if arguments.command == "disconnect":
        return connect.disconnect(arguments)
    if arguments.command == "daemon":
        return daemon.main(arguments)
    if arguments.command == "vm":
        return vm.main(arguments)
    if arguments.command == "doctor":
        return daemon.command_doctor(arguments)
    if arguments.command == "agent" and arguments.action in session.AGENTS:
        return session.main(arguments)
    if arguments.command in graph_commands.COMMANDS:
        return graph_commands.main(arguments)
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())

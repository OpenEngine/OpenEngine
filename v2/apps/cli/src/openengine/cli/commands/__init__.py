from __future__ import annotations

import argparse

from openengine.cli.commands import down, graph, logs, migrate, status, up

COMMANDS = (up, down, status, logs, migrate, graph)


def add_parsers(commands: argparse._SubParsersAction) -> None:
    for command in COMMANDS:
        command.add_parser(commands)

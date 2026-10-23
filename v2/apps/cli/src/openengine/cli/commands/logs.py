"""`oe logs`: print the supervisor's log, which its processes write to as well.

JSON log lines are printed in console form unless `--json` asks for them as written.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque

from openengine.cli.config.local import supervisor_log
from openengine.telemetry import render

FOLLOW_POLL_SECONDS = 0.2
EXIT_OK = 0
EXIT_FAILED = 1


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("logs", help="print the local OpenEngine log")
    parser.add_argument("-n", "--lines", type=int, default=100, help="how many trailing lines to print")
    parser.add_argument("-f", "--follow", action="store_true", help="keep printing new lines until interrupted")
    parser.add_argument("--json", action="store_true", help="print JSON log lines as written")
    parser.set_defaults(command=run)


def run(arguments: argparse.Namespace) -> int:
    path = supervisor_log()
    if not path.is_file():
        print(f"oe logs: no log at {path}; start OpenEngine with `oe up`.", file=sys.stderr)
        return EXIT_FAILED
    show = (lambda line: line) if arguments.json else render
    with path.open(errors="replace") as log:
        sys.stdout.writelines(map(show, deque(log, maxlen=arguments.lines)))
        sys.stdout.flush()
        try:
            while arguments.follow:
                if line := log.readline():
                    sys.stdout.write(show(line))
                    sys.stdout.flush()
                else:
                    time.sleep(FOLLOW_POLL_SECONDS)
        except KeyboardInterrupt:
            pass
    return EXIT_OK

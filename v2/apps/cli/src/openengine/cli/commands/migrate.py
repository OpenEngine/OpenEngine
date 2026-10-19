"""`oe migrate`: bring the running OpenEngine's durable state up to date.

For now that is NATS's JetStream streams. The services' databases join as
their migrations are written.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from openengine.cli.commands.status import running_state
from openengine.cli.migrations.streams import MigrationError, load_stream_configs, migrate_streams, streams_directory
from openengine.cli.output.render import table

EXIT_OK = 0
EXIT_FAILED = 1


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("migrate", help="create or update NATS streams on the running OpenEngine")
    parser.add_argument("--nats-url", help="migrate this NATS server instead of the one `oe up` started")
    parser.add_argument("--streams", type=Path, help="read stream configs from this directory instead of infra/nats/streams")
    parser.set_defaults(command=run)


def nats_url() -> str | None:
    state = running_state()
    return next((process.address for process in state.processes if process.name == "nats"), None) if state else None


def run(arguments: argparse.Namespace) -> int:
    url = arguments.nats_url or nats_url()
    if url is None:
        print("oe migrate: OpenEngine is not running; start it with `oe up`.", file=sys.stderr)
        return EXIT_FAILED
    try:
        configs = load_stream_configs(arguments.streams or streams_directory())
        results = asyncio.run(migrate_streams(url, configs))
    except MigrationError as error:
        print(f"oe migrate: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(table(["STREAM", "RESULT"], [[result.name, result.outcome] for result in results]))
    return EXIT_OK

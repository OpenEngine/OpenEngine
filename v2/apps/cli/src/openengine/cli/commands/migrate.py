"""`oe migrate`: bring the running OpenEngine's durable state up to date.

That is each service's database, on the backend `OE_DATABASE_BACKEND` selects,
and then NATS's JetStream streams.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from openengine.cli.commands.status import running_state
from openengine.cli.config.local import state_directory
from openengine.cli.migrations import MigrationError
from openengine.cli.migrations.databases import database_config, migrate_databases, outcome
from openengine.cli.migrations.streams import load_stream_configs, migrate_streams, streams_directory
from openengine.cli.output.render import table
from openengine.messaging.broker import BrokerTrust, load_trust

EXIT_OK = 0
EXIT_FAILED = 1


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser(
        "migrate", help="upgrade the services' databases and create or update NATS streams on the running OpenEngine"
    )
    parser.add_argument("--nats-url", help="migrate this NATS server instead of the one `oe up` started")
    parser.add_argument("--streams", type=Path, help="read stream configs from this directory instead of infra/nats/streams")
    parser.set_defaults(command=run)


def nats_url() -> str | None:
    state = running_state()
    return next((process.address for process in state.processes if process.name == "nats"), None) if state else None


class BrokerEndpointMismatch(ValueError):
    def __init__(self) -> None:
        super().__init__("managed broker endpoint does not match launch trust")


def _managed_trust(url: str) -> BrokerTrust:
    trust = load_trust(state_directory() / "broker.json")
    if url != trust.url:
        raise BrokerEndpointMismatch()
    return trust


def run(arguments: argparse.Namespace) -> int:
    url = arguments.nats_url or nats_url()
    if url is None:
        print("oe migrate: OpenEngine is not running; start it with `oe up`.", file=sys.stderr)
        return EXIT_FAILED
    try:
        # An explicit endpoint is external mode; managed mode requires its launch trust.
        trust = None if arguments.nats_url else _managed_trust(url)
        upgrades = migrate_databases(database_config())
        print(table(["DATABASE", "REVISION", "RESULT"], [[up.service, up.after or "-", outcome(up)] for up in upgrades]))
        configs = load_stream_configs(arguments.streams or streams_directory())
        results = asyncio.run(migrate_streams(url, configs, trust=trust))
    except (MigrationError, OSError, ValueError) as error:
        print(f"oe migrate: {error}", file=sys.stderr)
        return EXIT_FAILED
    print()
    print(table(["STREAM", "RESULT"], [[result.name, result.outcome] for result in results]))
    return EXIT_OK

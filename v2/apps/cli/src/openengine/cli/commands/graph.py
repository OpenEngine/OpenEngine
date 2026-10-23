"""`oe graph`: the graphs the Graph Engine has registered, asked over NATS."""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

import nats

from openengine.cli.commands.status import running_state
from openengine.cli.output.render import as_json, table
from openengine.messaging import Client, ServiceError, Unavailable

LIST_SUBJECT = "oe.v1.ge.query.graphs.list"
CONNECT_TIMEOUT_SECONDS = 2.0
EXIT_OK = 0
EXIT_FAILED = 1


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("graph", help="work with registered graphs")
    actions = parser.add_subparsers(required=True, metavar="action")
    listing = actions.add_parser("list", help="list the registered graphs")
    listing.add_argument("--json", action="store_true", help="print the query-response as JSON")
    listing.set_defaults(command=run_list)


class NotServing(Exception):
    """OpenEngine cannot answer: the reason, for the user."""


async def _query(nats_url: str, subject: str) -> dict[str, Any]:
    async def ignore(_: Exception) -> None:
        pass

    try:
        # nats-py retries a refused first connection forever unless it may make at least one reconnect attempt.
        connection = await nats.connect(
            nats_url, name="oe", connect_timeout=CONNECT_TIMEOUT_SECONDS, allow_reconnect=False,
            max_reconnect_attempts=1, reconnect_time_wait=0.1, error_cb=ignore,
        )
    except (OSError, nats.errors.Error):
        raise NotServing(f"cannot reach NATS at {nats_url}") from None
    try:
        return await Client(connection).query(subject)
    except Unavailable:
        raise NotServing("no reply from ge") from None
    finally:
        await connection.close()


def render(page: dict[str, Any]) -> str:
    if not page["items"]:
        return "No graphs registered."
    rows = [
        [graph["name"], str(graph["latest_revision"]), graph["updated_at"], graph.get("description", "")]
        for graph in page["items"]
    ]
    return table(["NAME", "REVISION", "UPDATED", "DESCRIPTION"], rows)


def run_list(arguments: argparse.Namespace) -> int:
    state = running_state()
    nats_url = next((process.address for process in state.processes if process.name == "nats"), None) if state else None
    try:
        if nats_url is None:
            raise NotServing("no supervisor")
        reply = asyncio.run(_query(nats_url, LIST_SUBJECT))
    except NotServing as reason:
        print(f"OpenEngine is not running ({reason}). Start it with `oe up`; see `oe logs`.", file=sys.stderr)
        return EXIT_FAILED
    except ServiceError as error:
        print(f"oe graph list: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(as_json(reply) if arguments.json else render(reply["result"]))
    return EXIT_OK

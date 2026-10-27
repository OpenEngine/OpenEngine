"""`oe graph`: the graphs the Graph Engine has registered, applied and asked over NATS, and offline checks of Graph files.

`apply` sends each Graph in a file to GE through the messaging client, which
resends an unanswered command with the same command ID, so a lost reply never
applies a graph twice. It then reads the operation to say whether the graph was
created, updated with a new revision, or unchanged.

`validate` and `spec` read only the Graph schema and rules in
openengine-contracts, so neither needs `oe up`, NATS or runner credentials.
Runners and tools are checked when a run starts, so a valid file is not a
promise that they exist.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import nats

from openengine.cli.commands.status import running_state
from openengine.cli.output.render import as_json, table
from openengine.contracts import expressions
from openengine.contracts.resources import load_documents, markdown, validate_text
from openengine.messaging import Client, ServiceError, Unavailable

KIND = "Graph"
STDIN = "-"
RUNTIME_NOTE = "note: runners and tools are not checked here; they are checked when a run starts"
LIST_SUBJECT = "oe.v1.ge.query.graphs.list"
APPLY_SUBJECT = "oe.v1.ge.control.graphs.apply"
OPERATION_SUBJECT = "oe.v1.ge.query.operations.{operation_id}.get"
CONNECT_TIMEOUT_SECONDS = 2.0
EXIT_OK = 0
EXIT_FAILED = 1


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("graph", help="work with registered graphs")
    actions = parser.add_subparsers(required=True, metavar="action")
    listing = actions.add_parser("list", help="list the registered graphs")
    listing.add_argument("--json", action="store_true", help="print the query-response as JSON")
    listing.set_defaults(command=run_list)
    apply = actions.add_parser("apply", help="register each Graph in a file, adding a revision when it changed")
    apply.add_argument("file", metavar="FILE", help=f"the Graph file to apply, or {STDIN} to read standard input")
    apply.add_argument("--json", action="store_true", help="print each command-response and its operation as JSON")
    apply.set_defaults(command=run_apply)
    validate = actions.add_parser("validate", help="check a Graph file against the Graph schema and rules, offline")
    validate.add_argument("file", metavar="FILE", help=f"the file to check, or {STDIN} to read standard input")
    validate.set_defaults(command=run_validate)
    spec = actions.add_parser("spec", help="print the Graph resource reference and expression syntax as Markdown")
    spec.set_defaults(command=run_spec)


class NotServing(Exception):
    """OpenEngine cannot answer: the reason, for the user."""


class NatsUnreachable(NotServing):
    def __init__(self, nats_url: str) -> None:
        super().__init__(f"cannot reach NATS at {nats_url}")


class NoReply(NotServing):
    def __init__(self, service: str) -> None:
        super().__init__(f"no reply from {service}")


T = TypeVar("T")


async def _ask(nats_url: str, ask: Callable[[Client], Awaitable[T]]) -> T:
    async def ignore(_: Exception) -> None:
        pass

    try:
        # nats-py retries a refused first connection forever unless it may make at least one reconnect attempt.
        connection = await nats.connect(
            nats_url, name="oe", connect_timeout=CONNECT_TIMEOUT_SECONDS, allow_reconnect=False,
            max_reconnect_attempts=1, reconnect_time_wait=0.1, error_cb=ignore,
        )
    except (OSError, nats.errors.Error):
        raise NatsUnreachable(nats_url) from None
    try:
        return await ask(Client(connection))
    except Unavailable:
        raise NoReply("ge") from None
    finally:
        await connection.close()


async def _query(nats_url: str, subject: str) -> dict[str, Any]:
    return await _ask(nats_url, lambda client: client.query(subject))


async def _apply(nats_url: str, graphs: list[Any]) -> list[dict[str, Any]]:
    async def apply(client: Client) -> list[dict[str, Any]]:
        applied = []
        for graph in graphs:
            response = await client.command(APPLY_SUBJECT, {"graph": graph})
            operation = await client.query(OPERATION_SUBJECT.format(operation_id=response["operation_id"]))
            applied.append({"response": response, "operation": operation["result"]["operation"]})
        return applied

    return await _ask(nats_url, apply)


def outcome(operation: dict[str, Any]) -> str:
    result = operation["result"]
    if not result["created"]:
        return "unchanged"
    return "created" if result["graph"]["revision"] == 1 else "updated"


def render(page: dict[str, Any]) -> str:
    if not page["items"]:
        return "No graphs registered."
    rows = [
        [graph["name"], str(graph["latest_revision"]), graph["updated_at"], graph.get("description", "")]
        for graph in page["items"]
    ]
    return table(["NAME", "REVISION", "UPDATED", "DESCRIPTION"], rows)


def _not_running(reason: str) -> int:
    print(f"OpenEngine is not running ({reason}). Start it with `oe up`; see `oe logs`.", file=sys.stderr)
    return EXIT_FAILED


def _nats_url() -> str | None:
    state = running_state()
    return next((process.address for process in state.processes if process.name == "nats"), None) if state else None


def run_list(arguments: argparse.Namespace) -> int:
    if (nats_url := _nats_url()) is None:
        return _not_running("no supervisor")
    try:
        reply = asyncio.run(_query(nats_url, LIST_SUBJECT))
    except NotServing as reason:
        return _not_running(str(reason))
    except ServiceError as error:
        print(f"oe graph list: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(as_json(reply) if arguments.json else render(reply["result"]))
    return EXIT_OK


def _read(name: str) -> str:
    if name == STDIN:
        return sys.stdin.read()
    with open(name, encoding="utf-8") as file:
        return file.read()


def run_validate(arguments: argparse.Namespace) -> int:
    name = arguments.file
    try:
        text = _read(name)
    except (OSError, UnicodeDecodeError) as error:
        print(f"{name}: cannot read: {error}")
        return EXIT_FAILED
    problems = validate_text(text, KIND)
    for problem in problems:
        print(f"{name}: {problem}")
    if problems:
        return EXIT_FAILED
    for document in load_documents(text):
        print(f"{name}: valid {KIND} {document['metadata']['name']}")
    print(RUNTIME_NOTE)
    return EXIT_OK


def run_apply(arguments: argparse.Namespace) -> int:
    name = arguments.file
    try:
        text = _read(name)
    except (OSError, UnicodeDecodeError) as error:
        print(f"{name}: cannot read: {error}", file=sys.stderr)
        return EXIT_FAILED
    if problems := validate_text(text, KIND):
        for problem in problems:
            print(f"{name}: {problem}", file=sys.stderr)
        return EXIT_FAILED
    if (nats_url := _nats_url()) is None:
        return _not_running("no supervisor")
    documents = load_documents(text)
    try:
        applied = asyncio.run(_apply(nats_url, documents))
    except NotServing as reason:
        return _not_running(str(reason))
    except ServiceError as error:
        print(f"oe graph apply: {error}", file=sys.stderr)
        return EXIT_FAILED
    for graph, document in zip(applied, documents):
        result = graph["operation"]["result"]["graph"]
        print(as_json(graph) if arguments.json else
              f"{outcome(graph['operation'])} {document['metadata']['name']} revision {result['revision']} (graph {result['graph_id']})")
    return EXIT_OK


def run_spec(arguments: argparse.Namespace) -> int:
    print(markdown(KIND) + "\n" + expressions.markdown(), end="")
    return EXIT_OK

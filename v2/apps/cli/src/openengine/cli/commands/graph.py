"""`oe graph`: the graphs the Graph Engine has registered, applied and asked over NATS, and offline checks of Graph files.

`apply` sends each Graph in a file to GE through the messaging client, which
resends an unanswered command with the same command ID, so a lost reply never
applies a graph twice. It then reads the operation to say whether the graph was
created, updated with a new revision, or unchanged.

`list` pages through `graphs.list`. `get NAME[@REVISION]` finds NAME's graph ID
with `graphs.list` and reads the revision, the latest by default, from
`graphs.{graph_id}.get`.

Each of the three acts in one project: `--project`, else `OE_PROJECT`, else
`default`. Graph names are unique only within a project, so `get` looks the
name up there alone.

`validate` and `spec` read only the Graph schema and rules in
openengine-contracts, so neither needs `oe up`, NATS or runner credentials.
Runners and tools are checked when a run starts, so a valid file is not a
promise that they exist.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import nats
import yaml

from openengine.cli.commands.status import running_state
from openengine.cli.output.render import as_json, table
from openengine.contracts import expressions
from openengine.contracts.resources import load_documents, markdown, validate_text
from openengine.messaging import Client, ErrorCode, ServiceError, Unavailable

KIND = "Graph"
STDIN = "-"
RUNTIME_NOTE = "note: runners and tools are not checked here; they are checked when a run starts"
LIST_SUBJECT = "oe.v1.ge.query.graphs.list"
GET_SUBJECT = "oe.v1.ge.query.graphs.{graph_id}.get"
APPLY_SUBJECT = "oe.v1.ge.control.graphs.apply"
OPERATION_SUBJECT = "oe.v1.ge.query.operations.{operation_id}.get"
PROJECT_VARIABLE = "OE_PROJECT"
DEFAULT_PROJECT = "default"
CONNECT_TIMEOUT_SECONDS = 2.0
EXIT_OK = 0
EXIT_FAILED = 1


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("graph", help="work with registered graphs")
    actions = parser.add_subparsers(required=True, metavar="action")
    listing = actions.add_parser("list", help="list the registered graphs")
    listing.add_argument("--json", action="store_true", help="print one page's query-response as JSON")
    listing.add_argument("--cursor", help="start from this next_cursor of an earlier page")
    _add_project(listing)
    listing.set_defaults(command=run_list)
    get = actions.add_parser("get", help="show a revision of a registered graph and its source")
    get.add_argument("graph", metavar="NAME[@REVISION]", help="the graph's name, and a revision if not the latest")
    get.add_argument("--json", action="store_true", help="print the query-response as JSON")
    _add_project(get)
    get.set_defaults(command=run_get)
    apply = actions.add_parser("apply", help="register each Graph in a file, adding a revision when it changed")
    apply.add_argument("file", metavar="FILE", help=f"the Graph file to apply, or {STDIN} to read standard input")
    apply.add_argument("--json", action="store_true", help="print each command-response and its operation as JSON")
    _add_project(apply)
    apply.set_defaults(command=run_apply)
    validate = actions.add_parser("validate", help="check a Graph file against the Graph schema and rules, offline")
    validate.add_argument("file", metavar="FILE", help=f"the file to check, or {STDIN} to read standard input")
    validate.set_defaults(command=run_validate)
    spec = actions.add_parser("spec", help="print the Graph resource reference and expression syntax as Markdown")
    spec.set_defaults(command=run_spec)


def _add_project(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project", metavar="NAME",
        help=f"the project whose graphs to use (default: ${PROJECT_VARIABLE}, else {DEFAULT_PROJECT})",
    )


def project(arguments: argparse.Namespace) -> str:
    """The project a command acts in: --project, else OE_PROJECT, else the default project."""
    return arguments.project or os.environ.get(PROJECT_VARIABLE) or DEFAULT_PROJECT


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


async def _pages(nats_url: str, project_: str, cursor: str | None, every: bool) -> list[dict[str, Any]]:
    async def pages(client: Client) -> list[dict[str, Any]]:
        replies = [await client.query(LIST_SUBJECT, {"project": project_} | ({"cursor": cursor} if cursor else {}))]
        while every and (cursor_ := replies[-1]["result"]["next_cursor"]) is not None:
            replies.append(await client.query(LIST_SUBJECT, {"project": project_, "cursor": cursor_}))
        return replies

    return await _ask(nats_url, pages)


class BadReference(ValueError):
    def __init__(self, reference: str) -> None:
        super().__init__(f"{reference!r} is not NAME or NAME@REVISION with a revision from 1")


def parse_reference(reference: str) -> tuple[str, int | None]:
    """`NAME` or `NAME@REVISION`, as the name and the revision; BadReference unless REVISION is a positive integer."""
    name, at, revision = reference.partition("@")
    if not name or (at and not (revision.isdigit() and int(revision) >= 1)):
        raise BadReference(reference)
    return name, int(revision) if at else None


async def _get(nats_url: str, project_: str, name: str, revision: int | None) -> dict[str, Any]:
    async def get(client: Client) -> dict[str, Any]:
        # Names sort before every longer name they prefix, so the graph named NAME, if any, comes first.
        params = {"project": project_, "name_prefix": name, "limit": 1}
        found = (await client.query(LIST_SUBJECT, params))["result"]["items"]
        if not found or found[0]["name"] != name:
            raise ServiceError(ErrorCode.NOT_FOUND, f"no graph named {name} in project {project_}")
        params = {} if revision is None else {"revision": revision}
        return await client.query(GET_SUBJECT.format(graph_id=found[0]["graph_id"]), params)

    return await _ask(nats_url, get)


async def _apply(nats_url: str, project_: str, graphs: list[Any]) -> list[dict[str, Any]]:
    async def apply(client: Client) -> list[dict[str, Any]]:
        applied = []
        for graph in graphs:
            response = await client.command(APPLY_SUBJECT, {"project": project_, "graph": graph})
            operation = await client.query(OPERATION_SUBJECT.format(operation_id=response["operation_id"]))
            applied.append({"response": response, "operation": operation["result"]["operation"]})
        return applied

    return await _ask(nats_url, apply)


def outcome(operation: dict[str, Any]) -> str:
    result = operation["result"]
    if not result["created"]:
        return "unchanged"
    return "created" if result["graph"]["revision"] == 1 else "updated"


def render(graphs: list[dict[str, Any]]) -> str:
    if not graphs:
        return "No graphs registered."
    rows = [
        [graph.get("project", DEFAULT_PROJECT), graph["name"], graph["graph_id"], str(graph["latest_revision"]), graph["updated_at"]]
        for graph in graphs
    ]
    return table(["PROJECT", "NAME", "GRAPH ID", "REVISION", "UPDATED"], rows)


class _Literal(yaml.SafeDumper):
    """Writes multi-line strings, such as prompts, as YAML literal blocks."""


_Literal.add_representer(str, lambda dumper, text: dumper.represent_scalar(
    "tag:yaml.org,2002:str", text, style="|" if "\n" in text else None))


def render_revision(result: dict[str, Any]) -> str:
    graph, revision = result["graph"], result["revision"]
    lines = [
        f"{graph['name']} revision {revision['revision']} of {graph['latest_revision']} in project {graph.get('project', DEFAULT_PROJECT)}"
        f" (graph {graph['graph_id']})",
        table(["format", revision["format"]], [["digest", f"sha256:{revision['source_sha256']}"],
                                               ["created", revision["created_at"]]]),
    ]
    if (source := revision.get("source")) is not None:
        # A Graph resource is kept as canonical JSON; it reads better as the YAML it was most likely written in.
        lines += ["", yaml.dump(json.loads(source), Dumper=_Literal, sort_keys=False, width=1 << 30).rstrip()
                  if revision["format"] == "resource" else source.rstrip()]
    return "\n".join(lines)


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
        replies = asyncio.run(_pages(nats_url, project(arguments), arguments.cursor, every=not arguments.json))
    except NotServing as reason:
        return _not_running(str(reason))
    except ServiceError as error:
        print(f"oe graph list: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(as_json(replies[0]) if arguments.json else render([graph for reply in replies for graph in reply["result"]["items"]]))
    return EXIT_OK


def run_get(arguments: argparse.Namespace) -> int:
    try:
        name, revision = parse_reference(arguments.graph)
    except BadReference as error:
        print(f"oe graph get: {error}", file=sys.stderr)
        return EXIT_FAILED
    if (nats_url := _nats_url()) is None:
        return _not_running("no supervisor")
    try:
        reply = asyncio.run(_get(nats_url, project(arguments), name, revision))
    except NotServing as reason:
        return _not_running(str(reason))
    except ServiceError as error:
        print(f"oe graph get: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(as_json(reply) if arguments.json else render_revision(reply["result"]))
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
    documents, project_ = load_documents(text), project(arguments)
    try:
        applied = asyncio.run(_apply(nats_url, project_, documents))
    except NotServing as reason:
        return _not_running(str(reason))
    except ServiceError as error:
        print(f"oe graph apply: {error}", file=sys.stderr)
        return EXIT_FAILED
    for graph, document in zip(applied, documents):
        result = graph["operation"]["result"]["graph"]
        print(as_json(graph) if arguments.json else
              f"{outcome(graph['operation'])} {document['metadata']['name']} revision {result['revision']}"
              f" in project {project_} (graph {result['graph_id']})")
    return EXIT_OK


def run_spec(arguments: argparse.Namespace) -> int:
    print(markdown(KIND) + "\n" + expressions.markdown(), end="")
    return EXIT_OK

"""Register local project defaults through GE; never mutate the checkout."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys

from openengine.cli.commands.graph import (
    _ask, _nats_url, _not_running, _add_project, project, NotServing,
)
from openengine.cli.output.render import as_json, table
from openengine.messaging import ServiceError


def add_parser(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("repo", help="register and inspect local project repositories")
    actions = parser.add_subparsers(required=True)
    for action in ("add", "list", "get"):
        command = actions.add_parser(action)
        _add_project(command)
        command.add_argument("--json", action="store_true")
        command.set_defaults(command=run, action=action)
        if action == "add":
            command.add_argument("checkout")
            command.add_argument("--base-ref", default="HEAD")
            command.add_argument("--workspace-policy", choices=["worktree"], default="worktree")
            trust = command.add_mutually_exclusive_group()
            trust.add_argument("--trust", dest="trusted", action="store_true")
            trust.add_argument("--no-trust", dest="trusted", action="store_false")
            command.set_defaults(trusted=None)


async def request(client, arguments):
    params = {"project": project(arguments)}
    if arguments.action == "add":
        payload = params | {"checkout": str(Path(arguments.checkout).expanduser().resolve()),
                            "base_ref": arguments.base_ref, "workspace_policy": arguments.workspace_policy,
                            "trusted": arguments.trusted}
        reply = await client.command("oe.v1.ge.control.repositories.add", payload)
        operation = await client.query(f"oe.v1.ge.query.operations.{reply['operation_id']}.get", {})
        return operation["result"]["operation"]["result"]
    return (await client.query(f"oe.v1.ge.query.repositories.{arguments.action}", params))["result"]


def run(arguments: argparse.Namespace) -> int:
    if (url := _nats_url()) is None:
        return _not_running("no supervisor")
    try:
        result = asyncio.run(_ask(url, lambda client: request(client, arguments)))
    except NotServing as error:
        return _not_running(str(error))
    except ServiceError as error:
        print(f"oe repo {arguments.action}: {error}", file=sys.stderr)
        return 1
    rows = result.get("items", [result.get("repository")])
    print(as_json(result) if arguments.json else table(
        ["CHECKOUT", "PROJECT", "BASE REF", "WORKSPACE POLICY", "TRUST"],
        [[row["checkout"], row["project"], row["base_ref"], row["workspace_policy"],
          "trusted" if row["trusted"] is True else "approval-required"] for row in rows]))
    return 0

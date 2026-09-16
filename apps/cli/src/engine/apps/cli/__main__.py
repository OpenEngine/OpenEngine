"""Submit work orders and follow their graph and agent messages."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from urllib.parse import quote

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.text import Text


async def request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    response = await client.request(method, path, **kwargs)
    check_response(response)
    return response.json()


def check_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = response.json().get("error", response.reason_phrase)
    except (ValueError, AttributeError):
        detail = response.reason_phrase
    raise RuntimeError(f"Daemon returned HTTP {response.status_code}: {detail}")


async def events(response: httpx.Response) -> AsyncIterator[dict]:
    """Decode SSE data frames, including multiline frames and keepalives."""
    data: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield json.loads("\n".join(data))
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


class Display:
    def __init__(self, console: Console, run_id: str) -> None:
        self.console = console
        self.run_id = run_id
        self.cursor = 0
        self.status = "connecting"
        self.error = ""
        self.approvals = False
        self.progress = Progress(
            TextColumn("{task.description}", markup=False),
            BarColumn(), TimeElapsedColumn(), console=console,
        )
        self.tasks: dict[str, int] = {}

    def event(self, event: dict) -> None:
        if event["sequence"] <= self.cursor:
            return
        self.cursor = event["sequence"]
        payload = event["payload"]
        if event["type"] == "transcript":
            label = f"{event.get('nodeId') or 'agent'} / {payload.get('role', 'assistant')}"
            self.console.print(Text(f"[{label}] {payload.get('text', '')}"))

    def snapshot(self, snapshot: dict) -> None:
        self.status = snapshot["status"]
        self.error = snapshot.get("error") or ""
        self.approvals = bool(snapshot.get("pendingApprovals"))
        active = {item["executionId"]: item["nodeId"]
                  for item in snapshot["activeExecutions"]}
        for execution in list(self.tasks):
            if execution not in active:
                self.progress.remove_task(self.tasks.pop(execution))
        for execution, node in active.items():
            if execution not in self.tasks:
                self.tasks[execution] = self.progress.add_task(node, total=None)

    def render(self) -> Group:
        parts = [Text(f"Work order {self.run_id} — {self.status}"), self.progress]
        if self.approvals:
            parts.append(Text("Awaiting approval. Open this work order in the web UI."))
        if self.error:
            parts.append(Text(self.error, style="red"))
        parts.append(Text("Ctrl-C detaches; execution continues in the daemon.", style="dim"))
        return Group(*parts)


async def stream_messages(client: httpx.AsyncClient, path: str, display: Display) -> None:
    while True:
        try:
            async with client.stream(
                "GET", path + "/events",
                headers={"Last-Event-ID": str(display.cursor)},
                timeout=httpx.Timeout(30, read=None),
            ) as response:
                if not response.is_success:
                    await response.aread()
                    check_response(response)
                async for event in events(response):
                    display.event(event)
        except httpx.TransportError:
            # Replay from the last displayed event after a broken connection.
            pass
        await asyncio.sleep(1)


async def watch(client: httpx.AsyncClient, run_id: str, console: Console) -> int:
    encoded = quote(run_id, safe="")
    path = f"/graph/api/runs/{encoded}"
    display = Display(console, run_id)
    stream = asyncio.create_task(stream_messages(client, path, display))
    try:
        with Live(display.render(), console=console, refresh_per_second=8) as live:
            while True:
                if stream.done():
                    stream.result()
                snapshot = await request(client, "GET", path)
                display.snapshot(snapshot)
                live.update(display.render())
                if snapshot["status"] in {"completed", "failed"}:
                    # The snapshot can win the race with the live feed. Drain the
                    # persisted transcript before detaching, including old runs.
                    stream.cancel()
                    with suppress(asyncio.CancelledError):
                        await stream
                    history = await request(client, "GET", f"/api/runs/{encoded}/graph-events")
                    for event in history["events"]:
                        display.event(event)
                    return 0 if snapshot["status"] == "completed" else 1
                await asyncio.sleep(0.5)
    finally:
        stream.cancel()
        with suppress(asyncio.CancelledError):
            await stream


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", default=os.getenv("ENGINE_URL", "http://localhost:8000"),
                        help="daemon base URL (default: ENGINE_URL or http://localhost:8000)")
    commands = result.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="submit and follow a work order")
    submit.add_argument("prompt")
    submit.add_argument("--repository", required=True)
    submit.add_argument("--workflow", required=True)
    submit.add_argument("--input", action="append", default=[], metavar="NAME=VALUE",
                        help="workflow input; repeat for multiple inputs")
    submit.add_argument("--no-watch", action="store_true", help="print the run ID and exit")
    follow = commands.add_parser("watch", help="follow an existing work order")
    follow.add_argument("run_id")
    commands.add_parser("workflows", help="list available workflow IDs")
    return result


async def run(args: argparse.Namespace, console: Console) -> int:
    headers = {}
    if cookie := os.getenv("ENGINE_COOKIE"):
        headers["Cookie"] = cookie
    async with httpx.AsyncClient(base_url=args.url.rstrip("/"), headers=headers, timeout=30) as client:
        if args.command == "workflows":
            config = await request(client, "GET", "/api/config")
            for workflow in config["workflows"]:
                console.print(Text(f"{workflow['id']}  {workflow['name']}"))
            return 0
        if args.command == "submit":
            inputs = {}
            for item in args.input:
                name, separator, value = item.partition("=")
                if not separator or not name.strip():
                    raise ValueError("--input must be NAME=VALUE")
                inputs[name] = value
            if not all(value.strip() for value in (args.prompt, args.repository, args.workflow)):
                raise ValueError("prompt, repository and workflow must not be blank")
            created = await request(client, "POST", "/api/runs", json={
                "prompt": args.prompt, "repository": args.repository,
                "workflowId": args.workflow, "inputs": inputs,
            })
            run_id = created["runId"]
            console.print(Text(run_id))
            if args.no_watch:
                return 0
        else:
            run_id = args.run_id
        return await watch(client, run_id, console)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args, Console()))
    except KeyboardInterrupt:
        Console(stderr=True).print("Detached. The work order continues in the daemon.")
        return 130
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        Console(stderr=True).print(Text(str(error), style="red"))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Watching a run in the terminal: its stage, its graph, and what the agent is saying.

    pair v1 · run-0123abcd · running · 1:42

      ✓ Plan ─── ⠹ Implement ─── ○ Review

      Graph  ✓ workspace → ✓ spec → ⠹ implement → ○ review
      Now    implement · claude · attempt 1

      ┃ I'll start by reading the retry helper and the tests that cover it.
      ┃ ⚙ Read tests/test_retry.py

Redrawn in place from the run's state on the backend, so nothing is lost by
detaching: Ctrl-C offers to detach or cancel, and `engine run watch` picks the
view back up. A question an agent stops on is asked here, inline.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
import time
from collections.abc import Callable, Sequence
from typing import Any, TextIO
from urllib.parse import quote

from engine.cli.http import Client

POLL_SECONDS = 1.0
FRAME_SECONDS = 0.15
MESSAGE_LINES = 8
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
STAGE_LABELS = {"plan": "Plan", "implement": "Implement", "review": "Review"}

_RESET, _BOLD, _DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
_GREEN, _RED, _YELLOW, _CYAN = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[36m"
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

Segment = tuple[str, str]
"""Some text and the style to draw it in."""


def visible(text: str) -> str:
    return _ANSI.sub("", text)


def status_mark(status: str, tick: int) -> Segment:
    return {
        "completed": ("✓", _GREEN),
        "done": ("✓", _GREEN),
        "running": (SPINNER[tick % len(SPINNER)], _CYAN),
        "active": (SPINNER[tick % len(SPINNER)], _CYAN),
        "awaiting_approval": ("?", _YELLOW),
        "failed": ("✗", _RED),
        "cancelled": ("■", _DIM),
        "interrupted": ("■", _DIM),
        "stopped": ("■", _DIM),
        "skipped": ("–", _DIM),
    }.get(status, ("○", _DIM))


def frame(run: dict[str, Any], *, tick: int = 0, width: int = 80, elapsed: float | None = None) -> list[list[Segment]]:
    """The view of one run state, as lines of styled segments. Pure, so it can be tested."""
    lines: list[list[Segment]] = []
    status = run["status"].replace("_", " ")
    header = f"{run['graph']} v{run['version']} · {run['runId']} · {status}"
    if elapsed is not None:
        header += f" · {int(elapsed // 60)}:{int(elapsed % 60):02d}"
    lines.append([(header, _BOLD)])
    lines.append([])

    stages: list[Segment] = [("  ", "")]
    for index, stage in enumerate(run.get("stages") or []):
        if index:
            stages.append((" ─── ", _DIM))
        mark, style = status_mark(stage["status"], tick)
        label_style = {"active": _BOLD + _CYAN, "done": _GREEN, "failed": _RED}.get(stage["status"], _DIM)
        stages += [(mark, style), (" " + STAGE_LABELS.get(stage["stage"], stage["stage"]), label_style)]
    lines.append(stages)
    lines.append([])

    nodes = {node["id"]: node for node in run.get("graphNodes") or []}
    graph: list[Segment] = [("  Graph  ", _DIM)]
    for index, level in enumerate(levels(list(nodes), run.get("edges") or [])):
        if index:
            graph.append((" → ", _DIM))
        for position, node_id in enumerate(level):
            if position:
                graph.append((" + ", _DIM))
            mark, style = status_mark(nodes[node_id]["status"], tick)
            current = node_id in (run.get("current") or [])
            graph += [(mark, style), (" " + node_id, _BOLD if current else "")]
    lines.append(graph)

    current = [nodes[node_id] for node_id in run.get("current") or [] if node_id in nodes]
    if current:
        for node in current:
            detail = " · ".join(
                part for part in (node["id"], node.get("runner") or "", f"attempt {node['attempt']}") if part
            )
            lines.append([("  Now    ", _DIM), (detail, _CYAN)])
    elif not run.get("terminal"):
        lines.append([("  Now    ", _DIM), ("between steps", _DIM)])

    speaking = current or [_last_heard(run)]
    for node in (node for node in speaking if node):
        message = node.get("latestMessage") or ""
        activity = node.get("latestActivity") or ""
        if not message and not activity:
            continue
        lines.append([])
        if len(speaking) > 1:
            lines.append([(f"  {node['id']}", _BOLD)])
        wrapped = [
            piece
            for paragraph in message.splitlines()
            for piece in (textwrap.wrap(paragraph, max(width - 6, 20)) or [""])
        ]
        style = "" if current else _DIM
        for piece in wrapped[-MESSAGE_LINES:]:
            lines.append([("  ┃ ", _DIM), (piece, style)])
        if activity and current:
            lines.append([("  ┃ ", _DIM), (f"⚙ {activity}", _YELLOW)])
    return lines


def levels(node_ids: Sequence[str], edges: Sequence[Sequence[str]]) -> list[list[str]]:
    """Nodes grouped by how many steps they are from the start, for drawing a DAG on one line."""
    depth = {node: 0 for node in node_ids}
    for _ in node_ids:
        for source, target in edges:
            if source in depth and target in depth:
                depth[target] = max(depth[target], depth[source] + 1)
    grouped: dict[int, list[str]] = {}
    for node in node_ids:
        grouped.setdefault(depth[node], []).append(node)
    return [grouped[key] for key in sorted(grouped)]


def _last_heard(run: dict[str, Any]) -> dict[str, Any] | None:
    finished = [node for node in run.get("graphNodes") or [] if node.get("latestMessage")]
    return finished[-1] if finished else None


class Terminal:
    """Draws frames in place, in colour when the terminal wants it."""

    def __init__(self, out: TextIO = sys.stdout) -> None:
        self.out = out
        self.color = out.isatty() and not os.environ.get("NO_COLOR")
        self.drawn = 0

    @property
    def width(self) -> int:
        return shutil.get_terminal_size((100, 24)).columns

    def draw(self, lines: list[list[Segment]]) -> None:
        self.clear()
        width = self.width - 1
        for line in lines:
            self.out.write(self._line(line, width) + "\n")
        self.drawn = len(lines)
        self.out.flush()

    def clear(self) -> None:
        if self.drawn:
            self.out.write(f"\x1b[{self.drawn}F\x1b[J")
        self.drawn = 0

    def keep(self) -> None:
        """Leave what is drawn on screen; the next frame starts below it."""
        self.drawn = 0

    def _line(self, segments: list[Segment], width: int) -> str:
        written, used = [], 0
        for text, style in segments:
            text = text[: max(width - used, 0)]
            used += len(text)
            written.append(f"{style}{text}{_RESET}" if style and self.color else text)
            if used >= width:
                break
        return "".join(written)


class Watch:
    """Follow one run until it ends, the person detaches, or they cancel it."""

    def __init__(
        self,
        client: Client,
        run_id: str,
        *,
        terminal: Terminal | None = None,
        ask: Callable[[str], str] = input,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.terminal = terminal or Terminal()
        self.ask = ask
        self.answered: set[str] = set()

    def follow(self) -> tuple[dict[str, Any], str]:
        """`(run, outcome)`: outcome is `finished`, `detached` or `cancelled`."""
        started = time.monotonic()
        tick = 0
        run = self._fetch()
        fetched = time.monotonic()
        out = self.terminal.out
        if self.terminal.color:
            out.write("\x1b[?25l")
        try:
            while True:
                try:
                    if time.monotonic() - fetched >= POLL_SECONDS:
                        run = self._fetch()
                        fetched = time.monotonic()
                    self.terminal.draw(frame(
                        run, tick=tick, width=self.terminal.width, elapsed=time.monotonic() - started,
                    ))
                    if run["terminal"]:
                        self.terminal.keep()
                        return run, "finished"
                    if self._approve(run):
                        run = self._fetch()
                        continue
                    time.sleep(FRAME_SECONDS)
                    tick += 1
                except KeyboardInterrupt:
                    outcome = self._interrupted()
                    if outcome == "detached":
                        return run, outcome
                    if outcome == "cancelled":
                        return self._fetch(), outcome
        finally:
            if self.terminal.color:
                out.write("\x1b[?25h")
                out.flush()

    def _fetch(self) -> dict[str, Any]:
        return self.client.get(f"/runs/{quote(self.run_id)}")

    def _approve(self, run: dict[str, Any]) -> bool:
        waiting = [item for item in run.get("pendingApprovals") or [] if item["approvalId"] not in self.answered]
        if not waiting:
            return False
        item = waiting[0]
        self.terminal.keep()
        what = item.get("command") or item.get("reason") or item.get("kind")
        self.terminal.out.write(f"\n{item['node']} asks to {item.get('kind', 'act').replace('_', ' ')}: {what}\n")
        if self.terminal.color:
            self.terminal.out.write("\x1b[?25h")
        self.terminal.out.flush()
        answer = ""
        while answer not in ("y", "n"):
            try:
                answer = self.ask("Allow? [y/n] ").strip().lower()[:1]
            except EOFError:
                answer = "n"
        self.answered.add(item["approvalId"])
        self.client.post(
            f"/runs/{quote(self.run_id)}/approvals/{quote(item['approvalId'])}",
            {"decision": "accept" if answer == "y" else "cancel"},
        )
        self.terminal.out.write("\n")
        return True

    def _interrupted(self) -> str:
        self.terminal.keep()
        if self.terminal.color:
            self.terminal.out.write("\x1b[?25h")
        self.terminal.out.write("\n")
        self.terminal.out.flush()
        try:
            answer = self.ask("[d]etach and leave it running, [c]ancel the run, or keep [w]atching? ").strip().lower()[:1]
        except (EOFError, KeyboardInterrupt):
            answer = "d"
        if answer == "c":
            self.client.post(f"/runs/{quote(self.run_id)}/cancel", {})
            return "cancelled"
        if answer == "w":
            return "watching"
        return "detached"


def summary(run: dict[str, Any]) -> list[str]:
    """What is worth reading once a run has ended: its final output and where the work is."""
    lines: list[str] = []
    nodes = run.get("graphNodes") or []
    sources = {source for source, _ in run.get("edges") or []}
    final = [node["id"] for node in nodes if node["id"] not in sources]
    for node_id in final:
        result = (run.get("results") or {}).get(node_id)
        if result:
            lines += ["", f"── {node_id} " + "─" * 20, str(result).strip()]
    workspace = run.get("workspace") or {}
    if workspace.get("path"):
        lines += ["", f"Changes are in {workspace['path']}" + (f" (branch {workspace['ref']})" if workspace.get("ref") else "")]
    cost = (run.get("usage") or {}).get("costUsd")
    if cost is not None:
        lines.append(f"Usage: ${cost:.2f}" + ("" if run["usage"].get("complete", True) else " (some costs unknown)"))
    return lines


__all__ = ["Terminal", "Watch", "frame", "levels", "summary", "visible"]

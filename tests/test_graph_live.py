"""Watching a run from the terminal: the frame drawn, approvals asked inline, and Ctrl-C."""

from __future__ import annotations

import io
from typing import Any

from engine.cli import live


def running(**changes: Any) -> dict[str, Any]:
    run = {
        "runId": "run-1", "graph": "pair", "version": 1, "status": "running", "terminal": False,
        "stages": [
            {"stage": "plan", "status": "done"},
            {"stage": "implement", "status": "active"},
            {"stage": "review", "status": "pending"},
        ],
        "graphNodes": [
            {"id": "spec", "status": "completed", "attempt": 1, "latestMessage": "spec written"},
            {"id": "implement", "status": "running", "runner": "claude", "attempt": 1,
             "latestMessage": "Reading the retry helper.\n" + "word " * 40, "latestActivity": "Read tests/x.py"},
            {"id": "security", "status": "pending", "attempt": 0},
            {"id": "bugs", "status": "pending", "attempt": 0},
        ],
        "edges": [["spec", "implement"], ["implement", "security"], ["implement", "bugs"]],
        "current": ["implement"],
    }
    run.update(changes)
    return run


def text(lines: list[list[live.Segment]]) -> str:
    return "\n".join("".join(piece for piece, _ in line) for line in lines)


def test_a_frame_shows_the_stages_the_graph_and_what_the_agent_is_saying() -> None:
    drawn = text(live.frame(running(), tick=2, width=60, elapsed=102))
    assert drawn.startswith("pair v1 · run-1 · running · 1:42")
    assert "✓ Plan ─── ⠹ Implement ─── ○ Review" in drawn
    assert "✓ spec → ⠹ implement → ○ security + ○ bugs" in drawn
    assert "Now    implement · claude · attempt 1" in drawn
    assert "┃ Reading the retry helper." in drawn and "┃ ⚙ Read tests/x.py" in drawn


def test_between_steps_and_after_the_end_it_shows_what_was_last_said() -> None:
    idle = text(live.frame(running(current=[])))
    assert "between steps" in idle and "⚙" not in idle
    finished = text(live.frame(running(current=[], terminal=True, status="completed")))
    assert "between steps" not in finished and "┃ Reading the retry helper." in finished


def test_several_current_nodes_are_each_named() -> None:
    run = running(current=["implement", "spec"])
    assert "  implement" in text(live.frame(run)).splitlines()


def test_every_status_has_a_mark() -> None:
    for status in ("completed", "awaiting_approval", "failed", "cancelled", "skipped", "unheard-of"):
        mark, _ = live.status_mark(status, 0)
        assert mark


def test_levels_group_nodes_by_distance_from_the_start() -> None:
    assert live.levels(["a", "b", "c", "d"], [["a", "b"], ["a", "c"], ["b", "d"], ["c", "d"]]) == [
        ["a"], ["b", "c"], ["d"],
    ]


def test_the_terminal_redraws_in_place_and_truncates_to_its_width(monkeypatch) -> None:
    out = io.StringIO()
    terminal = live.Terminal(out)
    terminal.color = True
    monkeypatch.setattr(live.shutil, "get_terminal_size", lambda fallback: type("S", (), {"columns": 11})())
    terminal.draw([[("0123456789abc", live._BOLD)], [("x", "")]])
    terminal.draw([[("again", "")]])
    written = out.getvalue()
    assert "\x1b[2F\x1b[J" in written  # the first frame was cleared before the second
    assert live.visible(written).splitlines()[0] == "0123456789"
    terminal.keep()
    assert terminal.drawn == 0


class Client:
    def __init__(self, *runs: dict[str, Any]) -> None:
        self.runs = list(runs)
        self.posted: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str) -> dict[str, Any]:
        return self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posted.append((path, body))
        return {}


def watch(client: Client, *answers: str) -> live.Watch:
    replies = iter(answers)
    terminal = live.Terminal(io.StringIO())
    terminal.color = True
    return live.Watch(client, "run-1", terminal=terminal, ask=lambda prompt: next(replies))


def test_a_watch_asks_for_approval_inline_then_follows_to_the_end(monkeypatch) -> None:
    monkeypatch.setattr(live, "FRAME_SECONDS", 0)
    monkeypatch.setattr(live, "POLL_SECONDS", 0)
    approval = {"approvalId": "a1", "node": "implement", "kind": "run_command", "command": "rm -rf build"}
    done = running(status="completed", terminal=True, current=[])
    client = Client(running(pendingApprovals=[approval]), running(pendingApprovals=[approval]), done)
    run, outcome = watch(client, "maybe", "y").follow()
    assert outcome == "finished" and run["status"] == "completed"
    assert client.posted == [("/runs/run-1/approvals/a1", {"decision": "accept"})]


def test_ctrl_c_can_detach_cancel_or_keep_watching(monkeypatch) -> None:
    monkeypatch.setattr(live, "FRAME_SECONDS", 0)
    interrupts = iter([True, True, False])

    def sleep(_: float) -> None:
        if next(interrupts, False):
            raise KeyboardInterrupt

    monkeypatch.setattr(live.time, "sleep", sleep)
    client = Client(running())
    run, outcome = watch(client, "w", "c").follow()
    assert outcome == "cancelled" and client.posted == [("/runs/run-1/cancel", {})]

    interrupts = iter([True])
    _, outcome = watch(Client(running()), "d").follow()
    assert outcome == "detached"


def test_a_closed_input_declines_and_detaches(monkeypatch) -> None:
    def closed(prompt: str) -> str:
        raise EOFError

    terminal = live.Terminal(io.StringIO())
    approval = {"approvalId": "a1", "node": "implement", "kind": "write"}
    client = Client(running())
    watcher = live.Watch(client, "run-1", terminal=terminal, ask=closed)
    assert watcher._approve(running(pendingApprovals=[approval]))
    assert client.posted == [("/runs/run-1/approvals/a1", {"decision": "cancel"})]
    assert not watcher._approve(running(pendingApprovals=[approval]))
    assert watcher._interrupted() == "detached"


def test_the_summary_shows_final_output_where_the_changes_are_and_cost() -> None:
    run = running(
        status="completed", terminal=True,
        results={"security": "no findings", "bugs": "", "implement": "done"},
        workspace={"path": "/tmp/checkout", "ref": "engine/ws-1"},
        usage={"costUsd": 1.234, "complete": False},
    )
    lines = live.summary(run)
    assert "no findings" in lines and "done" not in lines
    assert "Changes are in /tmp/checkout (branch engine/ws-1)" in lines
    assert "Usage: $1.23 (some costs unknown)" in lines
    assert live.summary({"graphNodes": [], "usage": {}}) == []

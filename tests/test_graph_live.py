"""Run watching preserves approval decisions and detach/cancel semantics."""

from copy import deepcopy
from io import StringIO
from os import terminal_size

import pytest

from engine.cli import live


def run_state(*, terminal=False):
    return {
        "graph": "pair", "version": 1, "runId": "run-1",
        "status": "completed" if terminal else "running", "terminal": terminal,
        "stages": [{"stage": "plan", "status": "done"}, {"stage": "review", "status": "active"}],
        "graphNodes": [
            {"id": "plan", "status": "completed", "attempt": 1, "latestMessage": "planned"},
            {"id": "review", "status": "completed" if terminal else "running", "attempt": 2,
             "runner": "stub", "latestMessage": "checking\n\nfinished", "latestActivity": "Read file.py"},
        ],
        "edges": [["plan", "review"]], "current": [] if terminal else ["review"],
        "results": {"plan": "intermediate", "review": "final findings"},
        "workspace": {"path": "/checkout", "ref": "agent/fix"},
        "usage": {"costUsd": 0.5, "complete": False},
    }


class Client:
    def __init__(self, run):
        self.run = run
        self.posts = []
        self.gets = []

    def get(self, path):
        self.gets.append(path)
        return deepcopy(self.run)

    def post(self, path, body):
        self.posts.append((path, body))
        self.run = run_state(terminal=True)


def test_frame_shows_progress_activity_and_final_output():
    run = run_state()
    text = "\n".join("".join(part for part, _ in line) for line in live.frame(run, elapsed=62))
    assert "pair v1 · run-1 · running · 1:02" in text
    assert "✓ Plan" in text and "Review" in text
    assert "review · stub · attempt 2" in text and "⚙ Read file.py" in text
    run["current"] = ["plan", "review"]
    assert any("".join(text for text, _ in line) == "  plan" for line in live.frame(run))
    run["current"] = []
    assert "between steps" in str(live.frame(run))
    final = run_state(terminal=True)
    assert "finished" in str(live.frame(final))
    result = "\n".join(live.summary(final))
    assert "final findings" in result and "intermediate" not in result
    assert "Changes are in /checkout (branch agent/fix)" in result
    assert "Usage: $0.50 (some costs unknown)" in result
    assert live.summary({}) == []
    assert live.levels(["a", "b", "c"], [["a", "b"], ["a", "c"]]) == [["a"], ["b", "c"]]


def test_terminal_redraw_clips_lines_and_respects_no_color(monkeypatch):
    out = StringIO()
    monkeypatch.setattr(out, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    terminal = live.Terminal(out)
    assert not terminal.color
    monkeypatch.delenv("NO_COLOR")
    terminal = live.Terminal(out)
    assert terminal.color
    monkeypatch.setattr(live.shutil, "get_terminal_size", lambda fallback: terminal_size((8, 24)))
    terminal.draw([[("hello", "\x1b[32m"), (" world", "")]])
    terminal.draw([[("done", "")]])
    assert "hello w\n" in live.visible(out.getvalue())
    assert "\x1b[1F\x1b[J" in out.getvalue()
    terminal.keep()
    assert terminal.drawn == 0


@pytest.mark.parametrize("answer,decision", [("yes", "accept"), ("no", "cancel"), (None, "cancel")])
def test_watch_posts_approval_once_and_finishes(answer, decision):
    run = run_state()
    run["pendingApprovals"] = [{"approvalId": "a-1", "node": "review", "kind": "run_command", "command": "pytest"}]
    client = Client(run)
    replies = iter(["invalid", answer])

    def ask(prompt):
        reply = next(replies)
        if reply is None:
            raise EOFError
        return reply

    terminal = live.Terminal(StringIO())
    terminal.color = True
    watch = live.Watch(client, "run-1", terminal=terminal, ask=ask)
    result, outcome = watch.follow()
    assert result["terminal"] and outcome == "finished"
    assert client.posts == [("/runs/run-1/approvals/a-1", {"decision": decision})]
    assert not watch._approve(run)
    assert "pytest" in terminal.out.getvalue()
    assert terminal.out.getvalue().endswith("\x1b[?25h")


@pytest.mark.parametrize("answer,outcome", [("d", "detached"), ("c", "cancelled"), ("w", "finished"), (None, "detached")])
def test_watch_interrupt_detaches_cancels_or_resumes(monkeypatch, answer, outcome):
    client = Client(run_state())
    sleeps = 0

    def sleep(seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            raise KeyboardInterrupt
        client.run = run_state(terminal=True)

    def ask(prompt):
        if answer is None:
            raise EOFError
        return answer

    monkeypatch.setattr(live.time, "sleep", sleep)
    monkeypatch.setattr(live, "POLL_SECONDS", 0)
    terminal = live.Terminal(StringIO())
    terminal.color = True
    _, actual = live.Watch(client, "run-1", terminal=terminal, ask=ask).follow()
    assert actual == outcome
    assert client.posts == ([("/runs/run-1/cancel", {})] if answer == "c" else [])
    assert len(client.gets) >= 2
    assert terminal.out.getvalue().endswith("\x1b[?25h")

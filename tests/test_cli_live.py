"""Pure terminal snapshots and live polling against a recording client."""

import io

import pytest

from engine.cli import live


def run_state(status="running"):
    return {
        "runId": "run-1", "graph": "pair", "version": 1, "status": status,
        "terminal": status in {"completed", "failed", "cancelled"},
        "stages": [{"stage": "plan", "status": "done"}, {"stage": "implement", "status": "active"}],
        "graphNodes": [
            {"id": "spec", "status": "completed", "attempt": 1, "executionId": "e1",
             "latestMessage": "Read the existing implementation.\nUse the retry helper."},
            {"id": "work", "status": status, "runner": "codex", "attempt": 2},
        ],
        "edges": [["spec", "work"]], "current": ["work"],
    }


def plain(run, width=80):
    return ["".join(text for text, _ in line) for line in live.frame(run, width=width)]


@pytest.mark.parametrize("status,mark", [("running", "⠋"), ("failed", "✗"), ("awaiting_approval", "?")])
def test_linear_frame(status, mark):
    assert live.levels(["spec", "work"], [["spec", "work"]]) == [["spec"], ["work"]]
    assert plain(run_state(status)) == [
        f"pair v1 · run-1 · {status.replace('_', ' ')}", "",
        "  ✓ Plan ─── ⠋ Implement", "", f"  Graph  ✓ spec → {mark} work",
        "  Now    work · codex · attempt 2",
        "  ✓ spec · Read the existing implementation.",
        "           Use the retry helper.",
    ]


def test_fan_out_levels_and_frame():
    run = run_state()
    run["graphNodes"] += [{"id": name, "status": "pending"} for name in ("security", "correctness", "join")]
    run["edges"] += [["work", "security"], ["work", "correctness"], ["security", "join"], ["correctness", "join"]]
    ids = [node["id"] for node in run["graphNodes"]]
    assert live.levels(ids, list(reversed(run["edges"]))) == [["spec"], ["work"], ["security", "correctness"], ["join"]]
    assert plain(run)[4] == "  Graph  ✓ spec → ⠋ work → ○ security + ○ correctness → ○ join"


@pytest.mark.parametrize("width,expected", [
    (24, ["  ✓ spec · Read the exi", "           Use the retr"]),
    (100, ["  ✓ spec · Read the existing implementation.", "           Use the retry helper."]),
])
def test_recent_completion_width(width, expected):
    assert plain(run_state(), width)[6:8] == expected


def test_latest_completion_uses_execution_time_and_result():
    run = run_state()
    run["graphNodes"].append({"id": "other", "status": "completed", "executionId": "e2"})
    run["nodes"] = [{"executionId": "e1", "finishedAt": "2026-10-12T12:02:00Z"},
                    {"executionId": "e2", "finishedAt": "2026-10-12T12:01:00Z"}]
    run["results"] = {"spec": "The final result.\nSecond line.\nOmit this."}
    assert plain(run)[6:8] == ["  ✓ spec · The final result.", "           Second line."]


class Client:
    def __init__(self, *runs):
        self.runs = iter(runs)
        self.posts = []

    def get(self, path):
        assert path == "/runs/run-1"
        return next(self.runs)

    def post(self, path, body):
        self.posts.append((path, body))


@pytest.mark.parametrize("answer,decision", [("y", "accept"), ("n", "cancel")])
def test_watch_answers_each_id_once_and_finishes(monkeypatch, answer, decision):
    run = run_state("awaiting_approval")
    run["pendingApprovals"] = [{"approvalId": "a1", "node": "work", "command": "pytest", "kind": "command"}]
    done = run_state("completed")
    client = Client(run, run, done)
    monkeypatch.setattr(live, "POLL_SECONDS", 0.01)
    clock = iter([0, 0, 0, 0, 0, 0, 1, 1, 1])
    monkeypatch.setattr(live.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(live.time, "sleep", lambda _: None)
    prompts = []
    watch = live.Watch(client, "run-1", terminal=live.Terminal(io.StringIO()),
                       ask=lambda prompt: prompts.append(prompt) or answer)
    assert watch.follow() == (done, "finished")
    assert len(prompts) == 1
    assert client.posts == [("/runs/run-1/approvals/a1", {"decision": decision})]


@pytest.mark.parametrize("answer,outcome", [("d", "detached"), ("c", "cancelled")])
def test_ctrl_c(monkeypatch, answer, outcome):
    run = run_state()
    cancelled = run_state("cancelled")
    client = Client(run, cancelled)

    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(live.time, "sleep", interrupt)
    watch = live.Watch(client, "run-1", terminal=live.Terminal(io.StringIO()), ask=lambda _: answer)
    assert watch.follow() == (cancelled if answer == "c" else run, outcome)
    assert client.posts == ([("/runs/run-1/cancel", {})] if answer == "c" else [])


UNTRUSTED = "danger\r\x1b[2K\x1b[1A\x1b]0;title\x07\x9b2K\b\t\u202e\nbenign"
ESCAPED = r"danger\r\x1b[2K\x1b[1A\x1b]0;title\x07\x9b2K\x08\t\u202e\nbenign"


@pytest.mark.parametrize("field", ["command", "reason", "kind"])
def test_approval_controls_are_visible_before_prompt(field):
    out = io.StringIO()
    client = Client()

    def ask(prompt):
        assert ESCAPED in out.getvalue()
        assert "\r" not in out.getvalue() and "\x1b" not in out.getvalue()
        return "y"

    watch = live.Watch(client, "run-1", terminal=live.Terminal(out), ask=ask)
    assert watch._approve({"pendingApprovals": [
        {"approvalId": "a1", "node": "work", field: UNTRUSTED},
    ]})
    assert client.posts == [("/runs/run-1/approvals/a1", {"decision": "accept"})]


def test_live_messages_results_and_summary_escape_controls():
    run = run_state()
    run["graph"] = UNTRUSTED
    run["graphNodes"][1]["latestMessage"] = UNTRUSTED
    run["graphNodes"][1]["latestActivity"] = UNTRUSTED
    run["results"] = {"spec": UNTRUSTED, "work": UNTRUSTED}
    run["failure"] = {"error": UNTRUSTED}
    run["workspace"] = {"path": UNTRUSTED, "ref": UNTRUSTED}
    rendered = "\n".join(plain(run, width=500))
    summary = "\n".join(live.summary(run, "local"))
    for output in (rendered, summary):
        assert ESCAPED.removesuffix(r"\nbenign") in output
        assert all(char not in output for char in ("\r", "\x1b", "\x9b", "\b", "\t", "\u202e"))
    assert "\nbenign" in summary


def test_terminal_escapes_content_but_preserves_owned_styles():
    terminal = live.Terminal(io.StringIO())
    terminal.color = True
    assert terminal._line([(UNTRUSTED, live._BOLD)], 500) == live._BOLD + ESCAPED + live._RESET
    assert terminal._line([("\x1b[2K", "")], 4) == r"\x1b"


def test_summary_points_to_workspace_details_without_printing_machine_paths():
    run = run_state("completed")
    run["workspace"] = {"path": "/private/tmp/engine-workspaces/ws-example", "ref": "engine/ws-example"}
    output = "\n".join(live.summary(run, "local"))
    assert "Workspace details: engine run get run-1 --backend local" in output
    assert run["workspace"]["path"] not in output
    assert run["workspace"]["ref"] not in output

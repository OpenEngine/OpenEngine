"""The terminal workbench: keys, the editor, the graph it draws, and its screens.

Driven without a terminal or a server: keys go into `App.handle`, service
answers come from a fake client, and background work runs inline.
"""

from __future__ import annotations

import re

import pytest

from engine.apps.cli.tui import model
from engine.apps.cli.tui.app import App, CONVERSATION, FORM, HOME, RUN
from engine.apps.cli.tui.editor import TextBuffer
from engine.apps.cli.tui.keys import Key, KeyDecoder


# --- keys --------------------------------------------------------------------


def decode(data: str) -> list[Key]:
    decoder = KeyDecoder()
    return decoder.feed(data) + decoder.flush()


@pytest.mark.parametrize(("data", "expected"), [
    ("\r", Key("enter")),
    ("\x1b[13;2u", Key("newline")),  # kitty Shift+Enter
    ("\x1b[27;2;13~", Key("newline")),  # xterm modifyOtherKeys Shift+Enter
    ("\x1b\r", Key("newline")),  # Option/Alt+Enter
    ("\n", Key("newline")),  # Ctrl+J, Windows Ctrl+Enter
    ("\x1b", Key("escape")),
    ("\x1b[27u", Key("escape")),  # kitty Escape
    ("\x1b[A", Key("up")),
    ("\x1bOB", Key("down")),
    ("\x1b[1;3D", Key("left", alt=True)),  # Option+Left
    ("\x1b[1;9C", Key("right", super=True)),  # Cmd+Right under kitty
    ("\x1b[1;5D", Key("left", ctrl=True)),
    ("\x1bb", Key("char", "b", alt=True)),  # Option+Left in macOS terminals
    ("\x1b\x7f", Key("backspace", alt=True)),  # Option+Backspace
    ("\x15", Key("char", "u", ctrl=True)),  # Cmd+Backspace in macOS terminals
    ("\x1b[127;9u", Key("backspace", super=True)),  # Cmd+Backspace under kitty
    ("\x1b[117;5u", Key("char", "u", ctrl=True)),
    ("\x1b[3~", Key("delete")),
    ("\x1b[Z", Key("backtab")),
    ("é", Key("char", "é")),
])
def test_every_spelling_of_a_key_decodes_to_it(data, expected):
    assert decode(data) == [expected]


def test_a_paste_is_text_even_when_it_contains_newlines():
    decoder = KeyDecoder()
    keys = decoder.feed("\x1b[200~one\rtwo")
    keys += decoder.feed("\x1b[20")
    keys += decoder.feed("1~x")
    assert keys == [Key("paste", "one\rtwo"), Key("char", "x")]


def test_an_escape_sequence_split_across_reads_is_one_key():
    decoder = KeyDecoder()
    assert decoder.feed("\x1b[1;") == []
    assert decoder.waiting
    assert decoder.feed("3C") == [Key("right", alt=True)]


# --- the editor --------------------------------------------------------------


def edited(text: str, cursor: int, *keys: Key) -> tuple[str, int]:
    buffer = TextBuffer(text)
    buffer.cursor = cursor
    for key in keys:
        buffer.handle(key)
    return buffer.text, buffer.cursor


def test_words_lines_and_the_whole_text_are_reachable():
    text = "fix the bug\nin saving"
    assert edited(text, 11, Key("left", alt=True))[1] == 8
    assert edited(text, 0, Key("char", "f", alt=True))[1] == 3
    assert edited(text, 5, Key("right", super=True))[1] == 11
    assert edited(text, 5, Key("char", "a", ctrl=True))[1] == 0
    assert edited(text, 14, Key("up"))[1] == 2
    assert edited(text, 2, Key("down"))[1] == 14
    assert edited(text, 5, Key("down", super=True))[1] == len(text)


def test_deleting_a_word_and_everything_before_the_cursor():
    text = "fix the bug\nin saving"
    assert edited(text, 11, Key("backspace", alt=True)) == ("fix the \nin saving", 8)
    assert edited(text, 11, Key("char", "w", ctrl=True)) == ("fix the \nin saving", 8)
    # Cmd+Backspace clears the whole window from the cursor back.
    assert edited(text, 15, Key("char", "u", ctrl=True)) == ("saving", 0)
    assert edited(text, 15, Key("backspace", super=True)) == ("saving", 0)
    assert edited(text, 0, Key("delete", alt=True)) == (" the bug\nin saving", 0)


def test_newlines_and_pastes_insert_text():
    assert edited("ab", 1, Key("newline")) == ("a\nb", 2)
    assert edited("", 0, Key("paste", "x\r\ny")) == ("x\ny", 3)


# --- the graph ---------------------------------------------------------------

TOPOLOGY = {
    "graphId": "g", "name": "G", "entryPoint": "workspace",
    "nodes": [
        {"nodeId": "workspace", "name": "Workspace", "kind": "workspace", "group": "Planning"},
        {"nodeId": "implementation", "name": "Implementation", "kind": "agent",
         "group": "Implementation", "alwaysOpen": True, "runner": "codex",
         "runnerInput": "implementation_runner"},
        {"nodeId": "review-a", "name": "Review (A)", "kind": "agent", "group": "Review",
         "runnerInput": "review_runner", "findingsKey": "review-a"},
        {"nodeId": "review-b", "name": "Review (B)", "kind": "agent", "group": "Review",
         "runnerInput": "review_runner", "findingsKey": "review-b"},
        {"nodeId": "reranker", "name": "Reranker", "kind": "agent", "group": "Review",
         "runnerInput": "implementation_runner", "findingsKey": "review"},
        {"nodeId": "human-review", "name": "Human review", "kind": "human", "group": "Review"},
    ],
    "edges": [
        {"source": "workspace", "target": "implementation"},
        {"source": "implementation", "target": "review-a"},
        {"source": "implementation", "target": "review-b"},
        {"source": "review-a", "target": "reranker"},
        {"source": "review-b", "target": "reranker"},
        {"source": "reranker", "target": "implementation"},
        {"source": "reranker", "target": "human-review"},
    ],
}


def events(*steps: tuple[str, str]) -> list[dict]:
    """(type, node) pairs, numbered, with an execution per start."""
    result, starts = [], {}
    for sequence, (kind, node) in enumerate(steps, start=1):
        if kind == "node.started":
            starts[node] = f"{node}-{sequence}"
        result.append({
            "sequence": sequence, "type": kind, "nodeId": node,
            "executionId": starts.get(node), "payload": {},
        })
    return result


LOOPED = events(
    ("node.started", "workspace"), ("node.finished", "workspace"),
    ("node.started", "implementation"), ("node.finished", "implementation"),
    ("node.started", "review-a"), ("node.started", "review-b"),
    ("node.finished", "review-a"), ("node.finished", "review-b"),
    ("node.started", "reranker"), ("node.finished", "reranker"),
    ("node.started", "implementation"),
)


def test_the_graph_is_a_chronologue_that_shows_a_loop_twice():
    snapshot = {"status": "running", "activeExecutions": [
        {"executionId": "implementation-11", "nodeId": "implementation"}]}
    entries = model.timeline(TOPOLOGY, LOOPED, snapshot)
    ran = [(entry.node_id, entry.status) for entry in entries if entry.status != model.PENDING]
    assert ran == [
        ("workspace", "done"), ("implementation", "done"), ("review-a", "done"),
        ("review-b", "done"), ("reranker", "done"), ("implementation", "running"),
    ]
    # Everything after the node now running is still to come.
    assert [entry.node_id for entry in entries if entry.status == model.PENDING] == [
        "review-a", "review-b", "reranker", "human-review",
    ]
    assert [entry.occurrence for entry in entries if entry.node_id == "implementation"] == [1, 2]


def test_nodes_of_a_state_are_tabbed_beneath_its_header():
    rows = model.rows(model.timeline(TOPOLOGY, LOOPED, None))
    drawn = [("  " if row.indent else "") + (row.label if not row.header else f"[{row.label}]")
             for row in rows]
    assert drawn[:9] == [
        "[Planning]", "  Workspace",
        "[Implementation]", "  Implementation",
        "[Review]", "  Review (A)", "  Review (B)", "  Reranker",
        "[Implementation]",
    ]


def test_a_new_workorder_says_which_policy_places_each_stage():
    rows = model.rows(
        model.timeline(TOPOLOGY, [], None),
        runner_values={"implementation_runner": "round-robin", "review_runner": "least-utilized"},
    )
    notes = {row.label: row.note for row in rows if row.note}
    assert notes["Implementation"] == "auto-select (round robin)"
    assert notes["Review (A)"] == "auto-select (least-used)"
    assert notes["Reranker"] == "auto-select (round robin)"


def test_a_group_shows_findings_where_its_nodes_keep_them():
    entries = [entry for entry in model.timeline(TOPOLOGY, LOOPED, None) if entry.group == "Review"]
    finding = {"tagline": "Saving drops data", "description": "It truncates.", "facet": "bugs"}
    sections = model.findings_for(entries[:3], {"review": [finding], "review-a": [finding], "review-b": []})
    assert [(one.title, len(one.findings)) for one in sections] == [
        ("Reranker", 1), ("Review (B)", 0), ("Review (A)", 1),
    ]


def test_only_a_working_or_always_open_node_can_be_written_to():
    snapshot = {"status": "completed", "activeExecutions": []}
    assert model.can_write(TOPOLOGY, snapshot, "implementation")
    assert not model.can_write(TOPOLOGY, snapshot, "review-a")
    working = {"activeExecutions": [{"executionId": "x", "nodeId": "review-a"}]}
    assert model.can_write(TOPOLOGY, working, "review-a")


def test_a_conversation_shows_steering_once_and_folds_diffs_into_tool_calls():
    conversation = model.conversation([
        {"sequence": 1, "type": "transcript", "payload": {"role": "user", "text": "Do it"}},
        {"sequence": 2, "type": "steering.received", "payload": {"message": "also tests"}},
        {"sequence": 3, "type": "transcript", "payload": {"role": "user", "text": "also tests"}},
        {"sequence": 4, "type": "tool.call", "payload": {"callId": "c", "name": "Edit a.py", "arguments": {
            "content": [{"type": "diff", "path": "a.py", "oldText": "x = 1\n", "newText": "x = 2\n"}]}}},
        {"sequence": 5, "type": "tool.result", "payload": {"callId": "c", "result": "completed"}},
        {"sequence": 6, "type": "transcript", "payload": {"role": "assistant", "text": "Done."}},
    ])
    assert [(item.kind, item.text) for item in conversation] == [
        ("user", "Do it"), ("user", "also tests"), ("tool", "Edit a.py"), ("assistant", "Done."),
    ]
    tool = conversation[2]
    assert tool.status == "completed"
    assert tool.diff == ["@ a.py  +1 -1", "-x = 1", "+x = 2"]


# --- the screens -------------------------------------------------------------


class FakeService:
    server = "http://127.0.0.1:4364"

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.steered: list[tuple[str, str, str]] = []
        self.decided: list[tuple[str, str, str]] = []
        self.snapshots: dict[str, dict] = {
            "run-1": {"status": "running", "activeExecutions": [
                {"executionId": "implementation-11", "nodeId": "implementation"}],
                "pendingApprovals": [], "values": {"inputs": {"implementation_runner": "codex"}}},
        }
        self.runs_list = [{"runId": "run-1", "name": "Fix saving", "workflowId": "g",
                           "workflowName": "G", "phase": "running_agent"}]

    def config(self):
        return {"workflows": [{"id": "g", "name": "G", "inputs": [
            {"name": "implementation_runner", "label": "Implementation runner", "default": "codex",
             "choices": ["codex", "claude", "least-utilized", "round-robin"]},
            {"name": "mode", "label": "Mode", "default": "connected",
             "choices": ["connected", "disconnected"]},
        ]}], "repositories": [{"name": "repo", "path": "/repo"}]}

    def source_control(self):
        return {"provider": "gh-cli", "ghCli": {"authenticated": True}}

    def runs(self):
        return list(self.runs_list)

    def topology(self, graph_id):
        return TOPOLOGY

    def run(self, run_id):
        return {"runId": run_id, "taskPrompt": "Fix saving"}

    def events(self, run_id, cursor):
        return [event for event in LOOPED if event["sequence"] > cursor] if run_id == "run-1" else []

    def snapshot(self, run_id):
        if run_id not in self.snapshots:
            raise RuntimeError("not found")
        return self.snapshots[run_id]

    def create_run(self, body):
        self.created.append(body)
        run = {"runId": "run-2", "name": "", "workflowId": "g", "workflowName": "G", "phase": "running_agent"}
        self.runs_list.insert(0, run)
        return run

    def steer(self, run_id, node_id, message):
        self.steered.append((run_id, node_id, message))
        return {}

    def decide(self, run_id, approval_id, decision):
        self.decided.append((run_id, approval_id, decision))
        return {}


def started(**options) -> tuple[App, FakeService]:
    service = FakeService()
    app = App(service, spawn=lambda work: work(), clock=lambda: 0.0, **options)  # type: ignore[arg-type]
    app.poll()
    app.drain()
    return app, service


def press(app: App, *keys: Key | str) -> None:
    for key in keys:
        if isinstance(key, str):
            for char in key:
                app.handle(Key("char", char))
        else:
            app.handle(key)
        app.drain()


def screen_text(app: App, width: int = 120, height: int = 40) -> str:
    lines, _cursor = app.render(width, height)
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines)


def test_new_workorder_is_a_prefilled_form_under_a_graph_preview():
    app, service = started()
    assert app.screen == HOME and app.home_index == 0
    text = screen_text(app)
    assert "New workorder" in text and "Implementation" in text
    press(app, Key("enter"))
    assert app.screen == FORM
    press(app, "Fix the saving bug", Key("newline"), "and test it")
    # Up through the task's two lines, past Mode, to the runner.
    press(app, Key("up"), Key("up"), Key("up"), Key("right"), Key("right"))
    assert "auto-select (least-used)" in screen_text(app)
    press(app, Key("enter"))
    assert service.created == [{
        "prompt": "Fix the saving bug\nand test it", "repository": "/repo", "workflowId": "g",
        "inputs": {"implementation_runner": "least-utilized", "mode": "connected"},
    }]
    assert app.screen == RUN and app.run_id == "run-2"


@pytest.mark.parametrize("options", [{"disconnected": True}, {}])
def test_disconnected_is_the_default_when_asked_for_or_the_forge_is_not_connected(options):
    service = FakeService()
    if not options:
        service.source_control = lambda: {"provider": "gh-cli", "ghCli": {"authenticated": False}}
    app = App(service, spawn=lambda work: work(), **options)  # type: ignore[arg-type]
    app.poll()
    app.drain()
    assert app.form_inputs()["mode"] == "disconnected"


def test_enter_walks_into_a_run_and_its_conversation_and_esc_walks_back_out():
    app, service = started()
    press(app, "j")
    assert "Fix saving" in screen_text(app)
    press(app, Key("enter"))
    assert app.screen == RUN
    # The node in progress is selected and its stream is on the right.
    assert "In progress" in screen_text(app)
    press(app, Key("enter"))
    assert app.screen == CONVERSATION and app.node_id == "implementation"
    press(app, "carry on", Key("newline"), "please")
    press(app, Key("escape"))
    assert app.screen == RUN
    press(app, Key("enter"))
    assert app.draft().text == "carry on\nplease"  # kept across Esc
    press(app, Key("enter"))
    assert service.steered == [("run-1", "implementation", "carry on\nplease")]
    assert app.draft().text == ""
    press(app, Key("escape"), Key("escape"))
    assert app.screen == HOME


def test_an_earlier_implementation_renews_it_and_a_finished_reviewer_is_read_only():
    app, service = started()
    press(app, "j", Key("enter"))
    rows = app.rows("run-1")
    first = next(index for index, row in enumerate(rows) if not row.header and row.entry.node_id == "implementation")
    reviewer = next(index for index, row in enumerate(rows) if not row.header and row.entry.node_id == "review-a")
    app.row_index = reviewer
    press(app, Key("enter"))
    assert not app.writable()
    press(app, "x")
    assert app.draft().text == ""
    assert "read-only" in screen_text(app)
    press(app, Key("escape"))
    app.row_index = first
    press(app, Key("enter"), "try again", Key("enter"))
    assert service.steered == [("run-1", "implementation", "try again")]


def test_a_review_group_shows_its_findings():
    app, service = started()
    finding = {"tagline": "Saving drops data", "description": "It truncates the file.",
               "facet": "bugs", "agent": "claude", "file": "save.py", "line": 3}
    service.snapshots["run-1"]["values"]["review"] = [finding]
    press(app, "j", Key("enter"))
    app.poll_run("run-1")
    app.drain()
    header = next(index for index, row in enumerate(app.rows("run-1")) if row.header and row.label == "Review")
    app.row_index = header
    text = screen_text(app)
    assert "Saving drops data" in text and "save.py:3 · bugs · claude" in text
    press(app, Key("enter"))
    assert app.screen == CONVERSATION and "Saving drops data" in screen_text(app)


def test_a_waiting_decision_is_answered_with_a_note():
    app, service = started()
    service.snapshots["run-1"]["pendingApprovals"] = [
        {"approvalId": "a-1", "nodeId": "implementation", "reason": "run tests"}]
    press(app, "j", Key("enter"))
    app.poll_run("run-1")
    app.drain()
    press(app, Key("enter"), "/approve fine by me", Key("enter"))
    assert service.steered == [("run-1", "implementation", "fine by me")]
    assert service.decided == [("run-1", "a-1", "accept")]


@pytest.mark.parametrize("size", [(40, 12), (80, 24), (200, 60)])
def test_every_screen_draws_at_any_size(size):
    app, _service = started()
    for keys in ([], [Key("enter")], [Key("escape"), "j", Key("enter")], [Key("enter")]):
        press(app, *keys)
        lines, _cursor = app.render(*size)
        assert len(lines) == size[1]


# --- the wheel ---------------------------------------------------------------


def test_the_mouse_is_left_to_the_terminal_and_the_wheel_sends_arrows():
    from engine.apps.cli.tui import terminal

    assert "\x1b[?1007h" in terminal.ENTER and "\x1b[?1007l" in terminal.LEAVE
    # No mouse reporting of any kind, so selecting and copying text still works.
    for mode in ("1000", "1002", "1003", "1006", "1015"):
        assert f"\x1b[?{mode}h" not in terminal.ENTER


def test_the_wheel_scrolls_a_conversation_past_the_composer():
    app, service = started()
    service.events = lambda run_id, cursor: [
        {"sequence": 1, "type": "node.started", "nodeId": "implementation", "executionId": "e", "payload": {}},
        *({"sequence": index, "type": "transcript", "nodeId": "implementation", "executionId": "e",
           "payload": {"role": "assistant", "text": f"line {index}"}} for index in range(2, 80)),
    ] if cursor == 0 else []
    service.snapshots["run-1"]["activeExecutions"] = [{"executionId": "e", "nodeId": "implementation"}]
    press(app, "j", Key("enter"), Key("enter"))
    assert app.screen == CONVERSATION and "line 79" in screen_text(app)
    for _ in range(30):
        press(app, Key("up"))
    assert "line 79" not in screen_text(app) and app.scroll == 30
    press(app, Key("down"))
    assert app.scroll == 29
    # Inside a multi-line draft the arrows move the cursor first.
    press(app, "one", Key("newline"), "two", Key("up"))
    assert app.scroll == 29 and app.draft().cursor == 3
    press(app, Key("up"))
    assert app.scroll == 30


def test_a_read_only_conversation_scrolls_with_the_arrows():
    app, _service = started()
    press(app, "j", Key("enter"))
    app.row_index = next(index for index, row in enumerate(app.rows("run-1"))
                         if not row.header and row.entry.node_id == "review-a")
    press(app, Key("enter"), Key("up"), Key("down"), Key("pageup"))
    assert app.screen == CONVERSATION and not app.writable()

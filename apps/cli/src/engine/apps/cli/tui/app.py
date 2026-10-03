"""The terminal workbench: WorkOrders, their graphs, and their conversations.

Four screens, walked with Enter and Esc:

    home          work orders | preview (graph, or the new-WorkOrder form)
    form          work orders | graph preview over the form, focused
    run           orders | graph | what is in progress, and what it is saying
    conversation  orders | graph | the node's conversation, focused

Every screen is the same three columns at different widths, so moving between
them slides the columns rather than replacing the screen.

The mouse is the workbench's: the wheel scrolls the pane under the pointer,
and dragging selects text inside the pane the drag started in -- never across
into its neighbours -- and copies it when the button is let go.

`App` holds the state and draws it; it never touches the terminal or the
network itself. Service calls go through `spawn` (a background thread in the
real workbench, inline in the tests) and come back as messages the main loop
hands to `apply`, so everything that changes what is on screen happens on one
thread.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from engine.apps.cli.tui import model
from engine.apps.cli.tui.client import ServiceClient
from engine.apps.cli.tui.editor import TextBuffer
from engine.apps.cli.tui.keys import Key
from engine.apps.cli.tui.text import (
    BOLD, CYAN, DIM, GREEN, MAGENTA, RED, SELECTED, YELLOW,
    Line, char_width, clip, plain, styled, wrap,
)

HOME, FORM, RUN, CONVERSATION = "home", "form", "run", "conversation"

MODE_INPUT = "mode"
DISCONNECTED = "disconnected"

GLYPHS = {
    model.RUNNING: ("●", YELLOW),
    model.WAITING: ("◆", MAGENTA),
    model.DONE: ("✓", GREEN),
    model.FAILED: ("✗", RED),
    model.STOPPED: ("■", DIM),
    model.PENDING: ("○", DIM),
}
SPINNER = "◐◓◑◒"

#: How long the columns take to slide between screens.
SLIDE_SECONDS = 0.18

PHASE_STATUS = {
    "scheduled": model.PENDING,
    "pending": model.RUNNING,
    "running_agent": model.RUNNING,
    "succeeded": model.DONE,
    "failed": model.FAILED,
}

Spawn = Callable[[Callable[[], None]], None]


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


@dataclass
class RunData:
    events: list[dict[str, Any]] = field(default_factory=list)
    cursor: int = 0
    snapshot: dict[str, Any] | None = None
    detail: dict[str, Any] | None = None
    error: str = ""
    loaded: bool = False


@dataclass
class Field:
    name: str
    label: str
    choices: list[str] = field(default_factory=list)
    value: str = ""
    text: TextBuffer | None = None
    """A free-text field: the task, or an input declared without choices."""

    @property
    def current(self) -> str:
        return self.text.text if self.text is not None else self.value


class App:
    def __init__(
        self,
        client: ServiceClient,
        *,
        disconnected: bool = False,
        local_repository: str = "",
        spawn: Spawn = _thread,
        clock: Callable[[], float] = time.monotonic,
        slide_seconds: float = SLIDE_SECONDS,
        copy: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.spawn = spawn
        self.clock = clock
        self.slide_seconds = slide_seconds
        self.copy = copy or (lambda _text: None)
        self.messages: queue.Queue[Callable[[], None]] = queue.Queue()
        self.prefer_disconnected = disconnected
        self.local_repository = local_repository
        self.screen = HOME
        self.quit = False
        self.status = ""
        self.status_error = False

        self.config: dict[str, Any] | None = None
        self.forge_connected: bool | None = None
        self.runs: list[dict[str, Any]] = []
        self.topologies: dict[str, dict[str, Any]] = {}
        self.data: dict[str, RunData] = {}

        self.home_index = 0
        self.run_id = ""
        self.row_index = 0
        self.node_id = ""
        self.group_rows: list[model.Entry] | None = None
        self.scroll = 0
        self.detail_scroll = 0
        # Whether each scrollable pane reads from its top or its bottom, as
        # last drawn, so a scroll key moves toward earlier content either way.
        self._anchors: dict[str, str] = {}
        self.drafts: dict[tuple[str, str], TextBuffer] = {}
        self.fields: list[Field] = []
        self.field_index = 0
        self.workflow_index = 0
        self.form_error = ""
        self._busy = False
        self._fetching: set[str] = set()
        # The column widths on screen, and the slide between two layouts.
        self._shown: list[int] = []
        self._slide_from: list[int] = []
        self._slide_to: list[int] = []
        self._slide_start = 0.0
        self._layout_width = 0
        # What was drawn last, per column: where it starts, how wide it is, and
        # its rows as plain text -- what a drag selects from.
        self._frame: list[tuple[int, int, list[str]]] = []
        self.selection: Selection | None = None

    @property
    def animating(self) -> bool:
        """Whether the next frames differ without anything else changing."""
        return self._shown != self._slide_to or bool(self.selection and self.selection.dragging)

    # --- data --------------------------------------------------------------

    def post(self, change: Callable[[], None]) -> None:
        self.messages.put(change)

    def drain(self) -> bool:
        changed = False
        while True:
            try:
                change = self.messages.get_nowait()
            except queue.Empty:
                return changed
            change()
            changed = True

    def watched(self) -> list[str]:
        """The runs whose feeds are worth reading right now."""
        ids = []
        if self.screen in (RUN, CONVERSATION) and self.run_id:
            ids.append(self.run_id)
        elif self.screen == HOME and self.home_index > 0:
            run = self.selected_run()
            if run is not None:
                ids.append(str(run.get("runId")))
        return ids

    def poll(self) -> None:
        """One round of reads. Runs on the poller thread; results are posted."""
        client = self.client
        config = self.config
        if config is None:
            try:
                config = client.config()
            except RuntimeError as error:
                self.post(lambda error=error: self._fail(str(error)))
                return
            try:
                status = client.source_control()
            except RuntimeError:
                status = {}
            self.post(lambda config=config: self._configured(config, status))
        try:
            runs = client.runs()
        except RuntimeError as error:
            self.post(lambda error=error: self._fail(str(error)))
            runs = self.runs
        else:
            self.post(lambda: self._set_runs(runs))
        wanted = {str(run.get("workflowId") or "") for run in runs}
        wanted |= {str(one.get("id") or "") for one in config.get("workflows") or [] if isinstance(one, dict)}
        wanted.discard("")
        for graph_id in wanted - set(self.topologies):
            try:
                topology = client.topology(graph_id)
            except RuntimeError:
                topology = {"graphId": graph_id, "nodes": [], "edges": [], "missing": True}
            self.post(lambda graph_id=graph_id, topology=topology: self.topologies.__setitem__(graph_id, topology))
        for run_id in self.watched():
            self.poll_run(run_id)

    def poll_run(self, run_id: str) -> None:
        data = self.data.get(run_id) or RunData()
        cursor = data.cursor
        client = self.client
        try:
            detail = client.run(run_id) if data.detail is None else None
            events = client.events(run_id, cursor)
        except RuntimeError as error:
            self.post(lambda error=error: self._run_error(run_id, str(error)))
            return
        try:
            snapshot = client.snapshot(run_id)
        except RuntimeError:
            # A scheduled WorkOrder has no graph run yet.
            snapshot = None
        self.post(lambda: self._run_update(run_id, cursor, detail, events, snapshot))

    def _fail(self, message: str) -> None:
        self.status, self.status_error = message, True

    def _configured(self, config: dict[str, Any], status: Mapping[str, Any]) -> None:
        self.config = config
        provider = status.get("provider") if isinstance(status, Mapping) else None
        gh = status.get("ghCli") if isinstance(status, Mapping) else None
        if not provider:
            self.forge_connected = False
        elif provider == "gh-cli":
            self.forge_connected = isinstance(gh, Mapping) and gh.get("authenticated") is True
        else:
            self.forge_connected = True
        self._build_form()

    def _set_runs(self, runs: list[dict[str, Any]]) -> None:
        selected = self.selected_run()
        self.runs = runs
        if selected is not None:
            for index, run in enumerate(runs):
                if run.get("runId") == selected.get("runId"):
                    self.home_index = index + 1
                    break
        self.home_index = min(self.home_index, len(self.runs))

    def _run_error(self, run_id: str, message: str) -> None:
        data = self.data.setdefault(run_id, RunData())
        data.error = message

    def _run_update(
        self, run_id: str, cursor: int, detail: dict[str, Any] | None,
        events: list[dict[str, Any]], snapshot: dict[str, Any] | None,
    ) -> None:
        data = self.data.setdefault(run_id, RunData())
        first = not data.loaded
        data.loaded = True
        data.error = ""
        if detail is not None:
            data.detail = detail
        if cursor == data.cursor:
            fresh = [event for event in events if int(event.get("sequence") or 0) > data.cursor]
            data.events.extend(fresh)
            if fresh:
                data.cursor = max(int(event.get("sequence") or 0) for event in fresh)
        data.snapshot = snapshot
        if first and run_id == self.run_id and self.screen == RUN:
            self._select_active()

    def _select_active(self) -> None:
        """Start on what is happening: the node in progress, if there is one."""
        rows = self.rows(self.run_id)
        active = next((index for index, row in enumerate(rows)
                       if not row.header and row.status in (model.RUNNING, model.WAITING)), None)
        if active is not None:
            self.row_index = active

    # --- the form ----------------------------------------------------------

    def workflows(self) -> list[dict[str, Any]]:
        workflows = (self.config or {}).get("workflows")
        return [one for one in workflows if isinstance(one, dict)] if isinstance(workflows, list) else []

    def workflow(self) -> dict[str, Any] | None:
        workflows = self.workflows()
        return workflows[min(self.workflow_index, len(workflows) - 1)] if workflows else None

    def repositories(self) -> list[str]:
        paths = [
            str(one.get("path")) for one in (self.config or {}).get("repositories") or []
            if isinstance(one, dict) and one.get("path")
        ]
        if self.local_repository and self.local_repository not in paths:
            paths.insert(0, self.local_repository)
        return paths

    def _build_form(self) -> None:
        task = next((one.text for one in self.fields if one.name == "task"), None) or TextBuffer()
        previous = {one.name: one.current for one in self.fields}
        fields: list[Field] = []
        workflows = self.workflows()
        if workflows:
            fields.append(Field(
                "workflow", "Workflow", [str(one.get("name") or one.get("id")) for one in workflows],
                str(self.workflow().get("name") or self.workflow().get("id")),  # type: ignore[union-attr]
            ))
        repositories = self.repositories()
        fields.append(Field(
            "repository", "Repository", repositories,
            previous.get("repository") or (repositories[0] if repositories else ""),
        ))
        for declared in (self.workflow() or {}).get("inputs") or []:
            name = str(declared.get("name"))
            choices = [str(choice) for choice in declared.get("choices") or []]
            default = str(declared.get("default") or "")
            if name == MODE_INPUT and DISCONNECTED in choices and (
                self.prefer_disconnected or self.forge_connected is False
            ):
                default = DISCONNECTED
            value = previous.get(name, default)
            if choices:
                fields.append(Field(name, str(declared.get("label") or name), choices,
                                    value if value in choices else default))
            else:
                fields.append(Field(name, str(declared.get("label") or name), text=TextBuffer(value)))
        fields.append(Field("task", "Task", text=task))
        self.fields = fields
        self.field_index = len(fields) - 1

    def form_inputs(self) -> dict[str, str]:
        declared = {str(one.get("name")) for one in (self.workflow() or {}).get("inputs") or []}
        return {one.name: one.current for one in self.fields if one.name in declared}

    def _cycle(self, field_: Field, step: int) -> None:
        if not field_.choices:
            return
        index = field_.choices.index(field_.value) if field_.value in field_.choices else 0
        field_.value = field_.choices[(index + step) % len(field_.choices)]
        if field_.name == "workflow":
            self.workflow_index = field_.choices.index(field_.value)
            self._build_form()
            self.field_index = 0

    def submit_form(self) -> None:
        task = next(one for one in self.fields if one.name == "task")
        repository = next((one.current for one in self.fields if one.name == "repository"), "")
        workflow = self.workflow()
        if workflow is None:
            self.form_error = "the service offers no workflows"
            return
        if not task.current.strip():
            self.form_error = "describe the task first"
            self.field_index = self.fields.index(task)
            return
        if not repository:
            self.form_error = "choose a repository"
            return
        body = {
            "prompt": task.current.strip(),
            "repository": repository,
            "workflowId": str(workflow.get("id")),
            "inputs": self.form_inputs(),
        }
        self.form_error = ""
        self._act("Creating WorkOrder", lambda: self.client.create_run(body), self._created)

    def _created(self, run: dict[str, Any]) -> None:
        for one in self.fields:
            if one.name == "task" and one.text is not None:
                one.text.clear()
        self.runs = [run, *[one for one in self.runs if one.get("runId") != run.get("runId")]]
        self.home_index = 1
        self.open_run(str(run.get("runId")))
        self._say(f"Started {run.get('name') or run.get('runId')}")

    # --- actions -----------------------------------------------------------

    def _act(self, label: str, call: Callable[[], Any], done: Callable[[Any], None]) -> None:
        if self._busy:
            self._fail("still sending the last request; try again in a moment")
            return
        self._busy = True
        self._say(f"{label}…")

        def work() -> None:
            try:
                result = call()
            except Exception as error:  # noqa: BLE001 -- never leave `_busy` set
                self.post(lambda error=error: self._finish(None, str(error) or type(error).__name__))
            else:
                self.post(lambda: self._finish(lambda: done(result), ""))

        self.spawn(work)

    def _finish(self, then: Callable[[], None] | None, error: str) -> None:
        self._busy = False
        if error:
            self._fail(error)
            return
        self.status = ""
        if then is not None:
            then()
        run_id = self.run_id
        if run_id:
            self.spawn(lambda: self.poll_run(run_id))

    def _say(self, message: str) -> None:
        self.status, self.status_error = message, False

    def selected_run(self) -> dict[str, Any] | None:
        if self.home_index <= 0 or self.home_index > len(self.runs):
            return None
        return self.runs[self.home_index - 1]

    def run(self, run_id: str | None = None) -> dict[str, Any] | None:
        run_id = run_id or self.run_id
        return next((one for one in self.runs if one.get("runId") == run_id), None)

    def open_run(self, run_id: str) -> None:
        self.run_id = run_id
        self.screen = RUN
        self.row_index = 0
        data = self.data.get(run_id)
        self._select_active()
        if data is None or not data.loaded:
            self.spawn(lambda: self.poll_run(run_id))

    def topology_for(self, run_id: str) -> dict[str, Any] | None:
        run = self.run(run_id)
        graph_id = str((run or {}).get("workflowId") or "")
        return self.topologies.get(graph_id)

    def entries(self, run_id: str) -> list[model.Entry]:
        data = self.data.get(run_id) or RunData()
        return model.timeline(self.topology_for(run_id), data.events, data.snapshot)

    def rows(self, run_id: str) -> list[model.Row]:
        return model.rows(self.entries(run_id))

    def draft(self) -> TextBuffer:
        return self.drafts.setdefault((self.run_id, self.node_id), TextBuffer())

    def writable(self) -> bool:
        if self.group_rows is not None:
            return False
        data = self.data.get(self.run_id) or RunData()
        return model.can_write(self.topology_for(self.run_id), data.snapshot, self.node_id)

    def send(self) -> None:
        text = self.draft().text.strip()
        if not text:
            return
        data = self.data.get(self.run_id) or RunData()
        run_id, node_id, buffer = self.run_id, self.node_id, self.draft()
        command, _, note = text.partition(" ")
        if command in ("/approve", "/reject"):
            waiting = model.pending_approvals(data.snapshot, node_id)
            if not waiting:
                self._fail("nothing here is waiting for a decision")
                return
            approval = str(waiting[0].get("approvalId"))
            decision = "accept" if command == "/approve" else "cancel"
            note = note.strip()

            def decide() -> None:
                if note:
                    self.client.steer(run_id, node_id, note)
                self.client.decide(run_id, approval, decision)

            self._act("Sending decision", decide, lambda _result: buffer.clear())
            return
        if not self.writable():
            self._fail("this node is read-only: it has finished and cannot be reopened")
            return
        def sent(_result: object) -> None:
            buffer.clear()
            self._say("Sent. It shows as queued until the agent takes it up.")

        self._act("Sending", lambda: self.client.steer(run_id, node_id, text), sent)

    # --- keys --------------------------------------------------------------

    def handle(self, key: Key) -> None:
        if key.name == "char" and key.ctrl and key.text == "c":
            self.quit = True
            return
        if key.mouse:
            self._mouse(key)
            return
        self.selection = None
        {HOME: self._home_key, FORM: self._form_key, RUN: self._run_key,
         CONVERSATION: self._conversation_key}[self.screen](key)

    def _home_key(self, key: Key) -> None:
        count = len(self.runs) + 1
        if _is_down(key):
            self.home_index = min(self.home_index + 1, count - 1)
        elif _is_up(key):
            self.home_index = max(self.home_index - 1, 0)
        elif key.is_char("g") or key.name == "home":
            self.home_index = 0
        elif key.is_char("G") or key.name == "end":
            self.home_index = count - 1
        elif key.is_char("q"):
            self.quit = True
        elif key.is_char("n"):
            self.home_index = 0
            self._open_form()
        elif key.name == "enter":
            if self.home_index == 0:
                self._open_form()
            else:
                run = self.selected_run()
                if run is not None:
                    self.open_run(str(run.get("runId")))

    def _open_form(self) -> None:
        if self.config is None:
            self._fail("still loading the service's configuration")
            return
        if not self.fields:
            self._build_form()
        self.screen = FORM
        self.field_index = len(self.fields) - 1

    def _form_key(self, key: Key) -> None:
        if key.name == "escape":
            self.screen = HOME
            return
        if not self.fields:
            return
        current = self.fields[self.field_index]
        if key.name == "enter":
            self.submit_form()
            return
        moving = key.name in ("tab", "backtab")
        if current.text is not None and not moving:
            multiline = current.name == "task"
            if key.name == "up" and key.plain and (not multiline or current.text.on_first_line):
                self.field_index = max(self.field_index - 1, 0)
                return
            if key.name == "down" and key.plain and (not multiline or current.text.on_last_line):
                self.field_index = min(self.field_index + 1, len(self.fields) - 1)
                return
            if key.name == "newline" and not multiline:
                return
            current.text.handle(key)
            self.form_error = ""
            return
        if key.name in ("down", "tab") or key.is_char("j"):
            self.field_index = min(self.field_index + 1, len(self.fields) - 1)
        elif key.name in ("up", "backtab") or key.is_char("k"):
            self.field_index = max(self.field_index - 1, 0)
        elif key.name == "right" or key.is_char("l") or key.is_char(" "):
            self._cycle(current, 1)
        elif key.name == "left" or key.is_char("h"):
            self._cycle(current, -1)

    def _run_key(self, key: Key) -> None:
        rows = self.rows(self.run_id)
        if key.name == "escape":
            self.screen = HOME
        elif _is_down(key):
            self._move_row(1)
        elif _is_up(key):
            self._move_row(-1)
        elif key.is_char("g") or key.name == "home":
            self._move_row(-len(rows))
        elif key.is_char("G") or key.name == "end":
            self._move_row(len(rows))
        elif key.is_char("q"):
            self.quit = True
        elif key.name in ("pageup", "pagedown"):
            self._scroll("detail", 10 if key.name == "pageup" else -10)
        elif key.name == "enter" and rows:
            row = rows[min(self.row_index, len(rows) - 1)]
            self.scroll = 0
            if row.header:
                self.group_rows = list(row.entries)
                self.node_id = ""
            else:
                self.group_rows = None
                self.node_id = row.entry.node_id
            self.screen = CONVERSATION

    def _conversation_key(self, key: Key) -> None:
        if key.name == "escape":
            # The draft stays where it was typed, keyed by run and node.
            self.screen = RUN
            return
        if key.name == "pageup":
            self._scroll("conversation", 10)
            return
        if key.name == "pagedown":
            self._scroll("conversation", -10)
            return
        writable = self.writable() or bool(
            model.pending_approvals((self.data.get(self.run_id) or RunData()).snapshot, self.node_id)
        )
        if not writable:
            if _is_up(key):
                self._scroll("conversation", 1)
            elif _is_down(key):
                self._scroll("conversation", -1)
            elif key.is_char("q"):
                self.quit = True
            return
        if key.name == "enter":
            self.send()
            return
        buffer = self.draft()
        # The wheel reaches a terminal program as ↑/↓ (see `terminal`), so
        # arrows past the composer's first or last line scroll the
        # conversation, the way a shell's history sits above its prompt.
        if _is_arrow(key, "up") and buffer.on_first_line:
            self._scroll("conversation", 1)
            return
        if _is_arrow(key, "down") and buffer.on_last_line:
            self._scroll("conversation", -1)
            return
        buffer.handle(key)

    def _move_row(self, step: int) -> None:
        rows = self.rows(self.run_id)
        self.row_index = _clamp(self.row_index + step, 0, max(len(rows) - 1, 0))
        self.detail_scroll = 0

    def _scroll(self, kind: str, earlier: int) -> None:
        """Move `earlier` lines toward the start of a pane's content."""
        attribute = "detail_scroll" if kind == "detail" else "scroll"
        step = earlier if self._anchors.get(kind, "bottom") == "bottom" else -earlier
        setattr(self, attribute, max(getattr(self, attribute) + step, 0))

    # --- the mouse ---------------------------------------------------------

    def _column_at(self, x: int) -> int | None:
        return next(
            (index for index, (start, width, _rows) in enumerate(self._frame)
             if width and start <= x < start + width),
            None,
        )

    def _mouse(self, key: Key) -> None:
        column = self._column_at(key.x)
        if key.name in ("wheelup", "wheeldown"):
            if column is not None:
                self._wheel(column, up=key.name == "wheelup")
            return
        if key.name == "press":
            self.selection = None
            if column is None or not 1 <= key.y <= len(self._frame[column][2]):
                return
            start = self._frame[column][0]
            point = (key.y - 1, key.x - start)
            self.selection = Selection(column, point, point)
            return
        selection = self.selection
        if selection is None or not selection.dragging:
            return
        start, width, rows = self._frame[selection.column]
        # Held to the column the drag began in, whatever the pointer crosses.
        selection.head = (_clamp(key.y - 1, 0, len(rows) - 1), _clamp(key.x - start, 0, width - 1))
        if key.name == "release":
            selection.dragging = False
            if selection.head == selection.anchor:
                self.selection = None  # A click, not a selection.
                return
            text = self.selected_text()
            if text.strip():
                self.copy(text)
                self._say(f"Copied {len(text)} characters")

    def _wheel(self, column: int, *, up: bool) -> None:
        """The wheel scrolls the pane under the pointer, not the focused one."""
        if column == 0:
            if self.screen in (HOME, FORM):
                self.screen = HOME
                self._home_key(Key("up" if up else "down"))
        elif column == 1:
            if self.screen in (RUN, CONVERSATION):
                self._move_row(-1 if up else 1)
        elif self.screen == RUN:
            self._scroll("detail", 3 if up else -3)
        elif self.screen == CONVERSATION:
            self._scroll("conversation", 3 if up else -3)

    def selected_text(self) -> str:
        if self.selection is None:
            return ""
        _start, width, rows = self._frame[self.selection.column]
        lines = []
        for row, first, last in self.selection.spans(width):
            lines.append(_cells(rows[row], first, last + 1).rstrip())
        return "\n".join(lines).strip("\n")

    # --- drawing -----------------------------------------------------------

    def _targets(self, width: int) -> list[int]:
        """Where each column settles on this screen."""
        if self.screen in (HOME, FORM):
            first = _clamp(width * 34 // 100, 24, 48)
            return [first, width - first - 1, 0]
        first = _clamp(width // 7, 12, 22)
        second = (
            _clamp(width * 42 // 100, 28, 60) if self.screen == RUN
            else _clamp(width // 5, 18, 30)
        )
        return [first, second, width - first - second - 2]

    def _widths(self, width: int) -> list[int]:
        """This frame's column widths: the target, or on the way to it."""
        target = self._targets(width)
        now = self.clock()
        if width != self._layout_width or not self._shown:
            # A resize is not a transition: settle at once.
            self._layout_width = width
            self._shown = self._slide_from = self._slide_to = target
            return target
        if target != self._slide_to:
            self._slide_from, self._slide_to, self._slide_start = self._shown, target, now
        progress = 1.0 if self.slide_seconds <= 0 else (now - self._slide_start) / self.slide_seconds
        if progress >= 1:
            self._shown = target
            return target
        eased = 1 - (1 - max(progress, 0.0)) ** 3
        first, second = (
            round(start + (end - start) * eased)
            for start, end in zip(self._slide_from[:2], target[:2])
        )
        third = width - first - second - 2
        if third < 4:
            first, second, third = first, width - first - 1, 0
        self._shown = [first, second, third]
        return self._shown

    def render(self, width: int, height: int) -> tuple[list[str], tuple[int, int] | None]:
        """The screen as lines of ANSI, and where the text cursor goes (0-based)."""
        self._cursor: tuple[int, int, int] | None = None
        body = height - 2
        widths = self._widths(width)
        drawers = [
            lambda w: self.draw_workorders(w, body, focused=self.screen == HOME, collapsed=w < 26),
            (
                (lambda w: self.draw_new_workorder(w, body, focused=self.screen == FORM))
                if self.screen in (HOME, FORM) and self.home_index == 0
                else (lambda w: self.draw_run_preview(w, body)) if self.screen in (HOME, FORM)
                else (lambda w: self.draw_graph(w, body, selected=self.row_index,
                                                condensed=self.screen == CONVERSATION))
            ),
            (
                (lambda w: self.draw_activity(w, body)) if self.screen == RUN
                else (lambda w: self.draw_conversation(w, body)) if self.screen == CONVERSATION
                else (lambda w: [])
            ),
        ]
        columns: list[tuple[int, int, list[Line]]] = []
        offset = 0
        for draw, column_width in zip(drawers, widths):
            if column_width <= 0:
                columns.append((offset, 0, []))
                continue
            if columns and any(one[1] for one in columns):
                offset += 1  # The separator.
            pane = [clip(line, column_width) for line in draw(column_width)[:body]]
            pane += [clip([], column_width) for _ in range(body - len(pane))]
            columns.append((offset, column_width, pane))
            offset += column_width
        self._frame = [
            (start, column_width, [plain(line) for line in pane])
            for start, column_width, pane in columns
        ]
        lines = [styled(clip(self.header_line(width), width, fill=SELECTED), SELECTED)]
        spans = {
            (self.selection.column, row): (first, last)
            for row, first, last in self.selection.spans(columns[self.selection.column][1])
        } if self.selection and columns[self.selection.column][1] else {}
        for row in range(body):
            line: Line = []
            for index, (_start, column_width, pane) in enumerate(columns):
                if not column_width:
                    continue
                if line:
                    line.append((DIM, "│"))
                cells = pane[row]
                if (index, row) in spans:
                    cells = _highlight(cells, *spans[(index, row)])
                line.extend(cells)
            lines.append(styled(line))
        lines.append(styled(clip(self.footer_line(), width - 1)))
        cursor = None
        if self._cursor is not None:
            pane_index, row, column = self._cursor
            start, column_width, _pane = columns[pane_index]
            if column_width:
                cursor = (row + 1, start + min(column, column_width - 1))
        return lines, cursor

    def header_line(self, width: int) -> Line:
        parts: Line = [(BOLD + ";" + SELECTED, " OpenEngine "), (SELECTED, f" {self.client.server} ")]
        if self.forge_connected is False:
            parts.append((SELECTED, " · forge not connected"))
        if self.screen in (RUN, CONVERSATION):
            run = self.run() or {}
            parts.append((SELECTED, f" · {_run_title(run)}"))
            snapshot = (self.data.get(self.run_id) or RunData()).snapshot or {}
            inputs = (snapshot.get("values") or {}).get("inputs") or {}
            if inputs.get(MODE_INPUT) == DISCONNECTED:
                parts.append((SELECTED, " · disconnected"))
        return parts

    def footer_line(self) -> Line:
        hints = {
            HOME: "j/k move · enter open · n new · q quit",
            FORM: "enter start · ↑/↓ fields · ←/→ choose · shift+enter newline · esc back",
            RUN: "j/k move · enter open · pgup/pgdn scroll details · esc back",
            CONVERSATION: (
                "enter send · shift/option+enter newline · wheel/pgup/pgdn scroll · esc back"
                if self.writable() else "wheel/↑/↓ scroll · esc back"
            ),
        }[self.screen]
        if self.status:
            return [(RED if self.status_error else CYAN, f" {self.status} "), (DIM, f"  {hints}")]
        return [(DIM, f" {hints}")]

    def draw_workorders(
        self, width: int, height: int, *, focused: bool, collapsed: bool = False,
    ) -> list[Line]:
        lines: list[Line] = [[(BOLD, " Work orders" if not collapsed else " Orders")], []]
        entries: list[Line] = []
        selected_line = 0
        current = self.home_index if self.screen in (HOME, FORM) else None
        for index in range(len(self.runs) + 1):
            if index == 0:
                label: Line = [(CYAN, " + "), (CYAN, "New workorder")]
            else:
                run = self.runs[index - 1]
                glyph, colour = self._run_glyph(run)
                label = [(colour, f" {glyph} "), ("", _run_title(run))]
            is_selected = (
                index == current if current is not None
                else index > 0 and self.runs[index - 1].get("runId") == self.run_id
            )
            if is_selected:
                selected_line = len(entries)
                style = SELECTED if focused else BOLD
                label = [(f"{style};{segment_style}" if segment_style else style, text)
                         for segment_style, text in clip(label, width)]
            entries.append(label)
        if self.config is None and not self.runs:
            entries.append([(DIM, " loading…")])
        return lines + _window(entries, height - len(lines), selected_line)

    def _run_glyph(self, run: Mapping[str, Any]) -> tuple[str, str]:
        progress = run.get("graphProgress") or {}
        status = PHASE_STATUS.get(str(run.get("phase")), model.RUNNING)
        if progress.get("waitingNodeIds"):
            status = model.WAITING
        glyph, colour = GLYPHS[status]
        if status == model.RUNNING:
            glyph = SPINNER[int(self.clock() * 4) % len(SPINNER)]
        return glyph, colour

    def draw_new_workorder(self, width: int, height: int, *, focused: bool) -> list[Line]:
        top = max(height // 2, 6)
        workflow = self.workflow()
        lines: list[Line] = [[(BOLD, " New workorder"), (DIM, f"  {workflow.get('name')}" if workflow else "")]]
        topology = self.topologies.get(str((workflow or {}).get("id") or ""))
        if topology is None:
            lines.append([(DIM, "  loading the workflow…" if workflow else "  no workflows offered")])
        else:
            entries = model.timeline(topology, [], None)
            rows = model.rows(entries, runner_values=self.form_inputs())
            lines.extend(self._graph_lines(rows, width, None, notes=True))
        lines = _window(lines, top, 0)
        lines.append([(DIM, "─" * width)])
        form, cursor = self._form_lines(width, height - len(lines), focused)
        if cursor is not None:
            self._cursor = (1, len(lines) + cursor[0], cursor[1])
        lines.extend(form)
        return lines

    def _form_lines(
        self, width: int, height: int, focused: bool,
    ) -> tuple[list[Line], tuple[int, int] | None]:
        """The form's lines, and where the cursor is among them."""
        lines: list[Line] = []
        cursor: tuple[int, int] | None = None
        label_width = max((len(one.label) for one in self.fields), default=8) + 2
        for index, one in enumerate(self.fields):
            active = focused and index == self.field_index
            marker = (CYAN, "› ") if active else ("", "  ")
            if one.name == "task":
                lines.append([marker, (BOLD, one.label)])
                buffer = one.text or TextBuffer()
                rows, (row, column) = buffer.layout(width - 4)
                if not buffer.text and not active:
                    rows = [""]
                visible = max(height - len(lines) - 2, 3)
                start = max(0, row - visible + 1)
                first = len(lines)
                for text in rows[start:start + visible]:
                    lines.append([("", "    "), ("", text)])
                if not buffer.text:
                    lines[-1] = [("", "    "), (DIM, "what should be done?")]
                if active:
                    cursor = (first + row - start, 4 + column)
                continue
            value = one.current
            shown = model.runner_label(value) if one.name.endswith("_runner") else value
            if one.choices:
                shown = f"‹ {shown} ›" if active else shown
            lines.append([marker, (DIM if not active else BOLD, one.label.ljust(label_width)), ("", shown)])
            if active and one.text is not None:
                cursor = (len(lines) - 1, 2 + label_width + len(value[: one.text.cursor]))
        if self.form_error:
            lines.append([(RED, f"  {self.form_error}")])
        return lines, cursor

    def draw_run_preview(self, width: int, height: int) -> list[Line]:
        run = self.selected_run()
        if run is None:
            return []
        run_id = str(run.get("runId"))
        lines: list[Line] = [
            [(BOLD, f" {_run_title(run)}")],
            [(DIM, f" {run.get('workflowName') or run.get('workflowId')} · {run.get('phase')} · {run.get('repository')}")],
            [],
        ]
        data = self.data.get(run_id)
        if data is None or not data.loaded:
            lines.append([(DIM, "  loading…")])
            return lines
        lines.extend(self._graph_lines(self.rows(run_id), width, None, notes=False))
        return lines

    def draw_graph(
        self, width: int, height: int, *, selected: int | None, condensed: bool = False,
    ) -> list[Line]:
        run = self.run() or {}
        title = " Workflow" if condensed else f" {run.get('workflowName') or 'Workflow'}"
        lines: list[Line] = [[(BOLD, title)], []]
        data = self.data.get(self.run_id)
        if data is None or not data.loaded:
            return lines + [[(DIM, "  loading…")]]
        if data.error:
            lines.append([(RED, f"  {data.error}")])
        rows = self.rows(self.run_id)
        if not rows:
            phase = str(run.get("phase") or "")
            lines.append([(DIM, "  scheduled; not started" if phase == "scheduled" else "  nothing has run yet")])
            return lines
        self.row_index = min(self.row_index, len(rows) - 1)
        drawn = self._graph_lines(rows, width, selected, notes=not condensed, condensed=condensed,
                                  focused=self.screen == RUN)
        focus = next((index for index, line in enumerate(drawn) if line and line[0][0].startswith(SELECTED)), 0)
        return lines + _window(drawn, height - len(lines), focus)

    def _graph_lines(
        self, rows: Sequence[model.Row], width: int, selected: int | None, *,
        notes: bool, condensed: bool = False, focused: bool = True,
    ) -> list[Line]:
        lines: list[Line] = []
        for index, row in enumerate(rows):
            if not condensed and index and not row.indent:
                lines.append([(DIM, "  │")])
            glyph, colour = GLYPHS[row.status]
            if row.status == model.RUNNING:
                glyph = SPINNER[int(self.clock() * 4) % len(SPINNER)]
            indent = "    " if row.indent else "  "
            if condensed:
                indent = "   " if row.indent else " "
            label: Line = [("", indent), (colour, glyph + " ")]
            if row.header:
                label.append((BOLD, row.label))
                if not condensed:
                    count = len(row.entries)
                    label.append((DIM, f"  {count} node{'s' if count != 1 else ''}"))
            else:
                entry = row.entry
                label.append(("" if entry.status != model.PENDING else DIM, entry.name))
                if entry.occurrence > 1:
                    label.append((DIM, f" ×{entry.occurrence}"))
                if not condensed and entry.runner and not notes:
                    label.append((DIM, f" · {entry.runner}"))
            if selected is not None and index == selected:
                style = SELECTED if focused else BOLD
                label = [(f"{style};{one}" if one else style, text) for one, text in clip(label, width)]
            lines.append(label)
            if notes and row.note:
                lines.append([(DIM, ("      " if row.indent else "    ") + "↳ " + row.note)])
        return lines

    def draw_activity(self, width: int, height: int) -> list[Line]:
        rows = self.rows(self.run_id)
        data = self.data.get(self.run_id) or RunData()
        if not rows:
            detail = data.detail or {}
            lines: list[Line] = [[(BOLD, " Task")]]
            for text in wrap(str(detail.get("taskPrompt") or ""), width - 2):
                lines.append([("", " " + text)])
            return lines
        row = rows[min(self.row_index, len(rows) - 1)]
        if row.header:
            self._anchors["detail"] = "top"
            lines = self._group_lines(row.entries, width, height, compact=True)
            self.detail_scroll = min(self.detail_scroll, max(len(lines) - height, 0))
            return _from_top(lines, height, self.detail_scroll)
        entry = row.entry
        self._anchors["detail"] = "bottom"
        if entry.status in (model.RUNNING, model.WAITING, model.PENDING):
            active = [one for one in self.entries(self.run_id)
                      if one.status in (model.RUNNING, model.WAITING)]
            if active:
                return self._in_progress_lines(active, width, height)
        lines = self._entry_lines(entry, width)
        self.detail_scroll = min(self.detail_scroll, max(len(lines) - height, 0))
        return _scrolled(lines, height, self.detail_scroll)

    def _in_progress_lines(self, active: Sequence[model.Entry], width: int, height: int) -> list[Line]:
        lines: list[Line] = [[(BOLD, " In progress")], []]
        share = max((height - 2) // max(len(active), 1), 4)
        for entry in active:
            glyph, colour = GLYPHS[entry.status]
            block: list[Line] = [[(colour, f" {glyph} "), (BOLD, entry.name),
                                  (DIM, f" · {entry.runner}" if entry.runner else "")]]
            if entry.status == model.WAITING:
                block.append([(MAGENTA, "   waiting for a decision — enter to answer")])
            items = model.conversation(self._occurrence_events(entry))
            stream = self._item_lines(items, width - 3, compact=True)
            block.extend([("", "   "), *line] for line in stream[-(share - len(block) - 1):] if share - len(block) - 1 > 0)
            block.append([])
            lines.extend(block)
        return lines[:height]

    def _entry_lines(self, entry: model.Entry, width: int) -> list[Line]:
        glyph, colour = GLYPHS[entry.status]
        lines: list[Line] = [[(colour, f" {glyph} "), (BOLD, entry.name),
                              (DIM, f" · {entry.status}" + (f" · {entry.runner}" if entry.runner else ""))], []]
        items = model.conversation(self._occurrence_events(entry))
        if not items:
            lines.append([(DIM, "   nothing said yet" if entry.status != model.PENDING else "   not reached yet")])
            return lines
        stream = self._item_lines(items, width - 3, compact=True)
        lines.extend([("", "   "), *line] for line in stream)
        return lines

    def _occurrence_events(self, entry: model.Entry) -> list[dict[str, Any]]:
        """This node's events from this time it started until it next started."""
        data = self.data.get(self.run_id) or RunData()
        events = model.node_events(data.events, entry.node_id)
        if not entry.first_sequence:
            return events
        later = [
            int(event.get("sequence") or 0) for event in events
            if event.get("type") == "node.started"
            and int(event.get("sequence") or 0) > entry.first_sequence
        ]
        end = min(later) if later else None
        return [
            event for event in events
            if int(event.get("sequence") or 0) >= entry.first_sequence
            and (end is None or int(event.get("sequence") or 0) < end)
        ]

    def _group_lines(
        self, entries: Sequence[model.Entry], width: int, height: int, *, compact: bool,
    ) -> list[Line]:
        data = self.data.get(self.run_id) or RunData()
        values = (data.snapshot or {}).get("values") or {}
        status = model.aggregate_status([one.status for one in entries])
        glyph, colour = GLYPHS[status]
        lines: list[Line] = [[(colour, f" {glyph} "), (BOLD, entries[0].group)], []]
        sections = model.findings_for(entries, values)
        if sections:
            for section in sections:
                lines.append([(BOLD, f" {section.title}"), (DIM, f"  {len(section.findings)}")])
                if not section.findings:
                    lines.append([(DIM, "   nothing found")])
                for finding in section.findings:
                    for index, text in enumerate(wrap(str(finding.get("tagline") or ""), width - 5)):
                        lines.append([("", "   "), (YELLOW if not index else "", "• " if not index else "  "), (BOLD, text)])
                    for text in wrap(str(finding.get("description") or ""), width - 5):
                        lines.append([("", "     "), (DIM, text)])
                    where = ":".join(str(part) for part in (finding.get("file"), finding.get("line")) if part)
                    lineage = " · ".join(str(part) for part in (where, finding.get("facet"), finding.get("agent")) if part)
                    if lineage:
                        lines.append([("", "     "), (CYAN, lineage)])
                lines.append([])
        for entry in entries:
            glyph, colour = GLYPHS[entry.status]
            lines.append([(colour, f" {glyph} "), ("", entry.name), (DIM, f" · {entry.status}")])
        return lines

    def _item_lines(self, items: Sequence[model.Item], width: int, *, compact: bool) -> list[Line]:
        lines: list[Line] = []
        for item in items:
            if item.kind == "user":
                texts = wrap(item.text, width - 2)
                if compact and len(texts) > 3:
                    texts = [*texts[:3], "…"]
                lines.extend([(CYAN, "› " if not index else "  "), (CYAN, text)] for index, text in enumerate(texts))
                if item.status == "queued":
                    lines.append([(DIM, "  queued · the agent reads it when its current step allows")])
                elif item.status == "delivered" and not compact:
                    lines.append([(DIM, "  delivered")])
            elif item.kind == "assistant":
                texts = wrap(item.text, width)
                if compact and len(texts) > 4:
                    texts = ["…", *texts[-4:]]
                lines.extend([("", text)] for text in texts)
            elif item.kind == "tool":
                status = f" · {item.status}" if item.status else ""
                lines.append([(DIM, "⚙ "), (DIM, item.text), (DIM, status)])
                diff = item.diff[:8] if compact else item.diff
                for text in diff:
                    colour = CYAN if text.startswith("@") else GREEN if text.startswith("+") else RED if text.startswith("-") else DIM
                    lines.append([("", "  "), (colour, text)])
                if compact and len(item.diff) > 8:
                    lines.append([(DIM, f"  … {len(item.diff) - 8} more lines")])
            elif item.kind == "approval":
                lines.append([(MAGENTA, "◆ "), (MAGENTA, item.text), (DIM, f" [{item.status}]")])
            elif item.kind == "error":
                lines.extend([(RED, text)] for text in wrap(item.text, width))
            else:
                lines.append([(DIM, f"── {item.text} ──")])
            if not compact:
                lines.append([])
        return lines

    def draw_conversation(self, width: int, height: int) -> list[Line]:
        data = self.data.get(self.run_id) or RunData()
        if self.group_rows is not None:
            self._anchors["conversation"] = "top"
            body = self._group_lines(self.group_rows, width, height, compact=False)
            self.scroll = min(self.scroll, max(len(body) - height, 0))
            return _from_top(body, height, self.scroll)
        self._anchors["conversation"] = "bottom"
        topology = self.topology_for(self.run_id) or {}
        node = next((one for one in topology.get("nodes") or [] if one.get("nodeId") == self.node_id), {})
        runner = model.resolved_runner(node, self.node_id, data.snapshot)
        state = "running" if model.is_active(data.snapshot, self.node_id) else "idle"
        header: list[Line] = [[(BOLD, f" {node.get('name') or self.node_id}"),
                               (DIM, f" · {state}" + (f" · {runner}" if runner else ""))], []]
        items = model.conversation(model.node_events(data.events, self.node_id))
        body = self._item_lines(items, width - 2, compact=False)
        body = [[("", " "), *line] for line in body]
        if not body:
            body = [[(DIM, " nothing said yet")]]
        waiting = model.pending_approvals(data.snapshot, self.node_id)
        footer: list[Line] = []
        for approval in waiting:
            detail = approval.get("command") or approval.get("toolName") or ""
            footer.append([(MAGENTA, " ◆ waiting: "), ("", str(approval.get("reason") or "")),
                           (DIM, f"  {detail}" if detail else "")])
            footer.append([(DIM, "   /approve or /reject, optionally followed by a note")])
        footer.append([(DIM, "─" * width)])
        if self.writable() or waiting:
            buffer = self.draft()
            rows, (row, column) = buffer.layout(width - 3)
            visible = rows[max(0, row - 7): max(0, row - 7) + 8]
            top = len(footer)
            for index, text in enumerate(visible):
                footer.append([(CYAN, " › " if not index else "   "), ("", text)])
            if not buffer.text:
                footer[-1] = [(CYAN, " › "), (DIM, "message this node" if self.writable() else "/approve or /reject")]
            cursor_row = height - (len(footer) - top) + (row - max(0, row - 7))
            self._cursor = (2, cursor_row, 3 + column)
        else:
            footer.append([(DIM, " read-only: this node has finished and cannot be reopened")])
        room = max(height - len(header) - len(footer), 1)
        self.scroll = min(self.scroll, max(len(body) - room, 0))
        middle = _scrolled(body, room, self.scroll)
        return header + middle + footer


@dataclass
class Selection:
    """A drag in one column, in that column's own (row, cell) coordinates."""

    column: int
    anchor: tuple[int, int]
    head: tuple[int, int]
    dragging: bool = True

    def spans(self, width: int) -> list[tuple[int, int, int]]:
        """(row, first cell, last cell) for every row the selection covers."""
        (top, left), (bottom, right) = sorted((self.anchor, self.head))
        if top == bottom:
            return [(top, left, right)]
        return [
            (row, left if row == top else 0, right if row == bottom else width - 1)
            for row in range(top, bottom + 1)
        ]


def _cells(text: str, first: int, end: int) -> str:
    """The characters of `text` occupying cells `first` up to `end`."""
    out, cell = [], 0
    for char in text:
        size = char_width(char)
        if first <= cell < end:
            out.append(char)
        cell += size
    return "".join(out)


def _highlight(line: Line, first: int, last: int) -> Line:
    """`line` with cells `first`..`last` shown reversed, keeping their colours."""
    out: Line = []
    cell = 0
    for style, text in line:
        for char in text:
            chosen = f"{style};{SELECTED}" if style else SELECTED
            out.append((chosen if first <= cell <= last else style, char))
            cell += char_width(char)
    return out


def _run_title(run: Mapping[str, Any]) -> str:
    name = str(run.get("name") or "").strip()
    return name or f"{run.get('workflowName') or 'WorkOrder'} {str(run.get('runId') or '')[-6:]}"


def _is_arrow(key: Key, name: str) -> bool:
    return key.name == name and key.plain


def _is_down(key: Key) -> bool:
    return (key.name == "down" and key.plain) or key.is_char("j")


def _is_up(key: Key) -> bool:
    return (key.name == "up" and key.plain) or key.is_char("k")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _window(lines: list[Line], height: int, focus: int) -> list[Line]:
    """`height` lines of `lines` that keep line `focus` in view."""
    if height <= 0:
        return []
    if len(lines) <= height:
        return lines + [[] for _ in range(height - len(lines))]
    start = _clamp(focus - height // 2, 0, len(lines) - height)
    return lines[start:start + height]


def _from_top(lines: list[Line], height: int, offset: int) -> list[Line]:
    """`height` lines starting `offset` lines down."""
    offset = _clamp(offset, 0, max(len(lines) - height, 0))
    shown = lines[offset:offset + height]
    return shown + [[] for _ in range(height - len(shown))]


def _scrolled(lines: list[Line], height: int, scroll: int) -> list[Line]:
    """The last `height` lines, `scroll` lines up from the bottom."""
    if len(lines) <= height:
        return lines + [[] for _ in range(height - len(lines))]
    end = len(lines) - _clamp(scroll, 0, len(lines) - height)
    return lines[end - height:end]


__all__ = ["App"]

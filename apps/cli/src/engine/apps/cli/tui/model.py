"""What the workbench draws, worked out from what the service reports.

Nothing here talks to a terminal or a server. A run is its graph's topology,
its event feed (`/api/runs/{run}/graph-events`) and its latest snapshot
(`/graph/api/runs/{run}`), and every function takes those as plain JSON so the
tests can hand them in directly.

The graph a run is drawn as is a *chronologue*: one entry per time a node
started, in the order they started, so a run that went back to implementation
shows implementation twice. After the history come the nodes the run has not
reached yet, dimmed, so the whole configured workflow is always on screen.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The runner policies a workflow input may name. Spelled here rather than
#: imported: the CLI is installed on its own, without the graph runtime.
LEAST_UTILIZED = "least-utilized"
ROUND_ROBIN = "round-robin"

RUNNING = "running"
WAITING = "waiting"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
PENDING = "pending"

#: How a runner policy reads under the nodes it places.
POLICY_LABELS = {
    ROUND_ROBIN: "auto-select (round robin)",
    LEAST_UTILIZED: "auto-select (least-used)",
}

TERMINAL_RUN_STATUSES = {"completed", "failed"}


def runner_label(value: str) -> str:
    return POLICY_LABELS.get(value, value)


@dataclass
class Entry:
    """One time one node ran, or a node the run has yet to reach."""

    node_id: str
    name: str
    kind: str = "agent"
    group: str = ""
    execution_id: str = ""
    status: str = PENDING
    occurrence: int = 1
    runner: str = ""
    runner_input: str = ""
    always_open: bool = False
    findings_key: str = ""
    first_sequence: int = 0

    @property
    def key(self) -> str:
        return self.execution_id or f"{self.node_id}#{self.occurrence}:{self.status}"


@dataclass
class Row:
    """One line of the graph pane: a group header or a node beneath one."""

    entries: list[Entry]
    header: bool = False
    indent: bool = False
    note: str = ""
    """What to write under this row: which runner, or which policy picks it."""

    @property
    def entry(self) -> Entry:
        return self.entries[0]

    @property
    def status(self) -> str:
        return aggregate_status([entry.status for entry in self.entries])

    @property
    def label(self) -> str:
        return self.entry.group if self.header else self.entry.name


def aggregate_status(statuses: Sequence[str]) -> str:
    for status in (WAITING, RUNNING, FAILED, STOPPED):
        if status in statuses:
            return status
    if statuses and all(status == PENDING for status in statuses):
        return PENDING
    if PENDING in statuses:
        # Some of the group has run and some has not: it is under way.
        return RUNNING if any(status == DONE for status in statuses) else PENDING
    return DONE


def node_order(topology: Mapping[str, Any]) -> list[str]:
    """The nodes top-down: breadth first from the entry point, loops not followed back."""
    nodes = [str(node.get("nodeId")) for node in topology.get("nodes", [])]
    edges: dict[str, list[str]] = {}
    for edge in topology.get("edges", []):
        edges.setdefault(str(edge.get("source")), []).append(str(edge.get("target")))
    entry = str(topology.get("entryPoint") or (nodes[0] if nodes else ""))
    order: list[str] = []
    queue = [entry] if entry in nodes else []
    while queue:
        current = queue.pop(0)
        if current in order:
            continue
        order.append(current)
        queue.extend(target for target in edges.get(current, []) if target not in order)
    # Anything unreachable from the entry still belongs to the graph.
    order.extend(node for node in nodes if node not in order)
    return order


def _node_map(topology: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(node.get("nodeId")): node for node in topology.get("nodes", [])}


def _entry_for(node: Mapping[str, Any], node_id: str, **values: Any) -> Entry:
    return Entry(
        node_id=node_id,
        name=str(node.get("name") or node_id),
        kind=str(node.get("kind") or "agent"),
        group=str(node.get("group") or ""),
        runner_input=str(node.get("runnerInput") or ""),
        always_open=bool(node.get("alwaysOpen")),
        findings_key=str(node.get("findingsKey") or ""),
        **values,
    )


def resolved_runner(
    node: Mapping[str, Any], node_id: str, snapshot: Mapping[str, Any] | None,
) -> str:
    if node.get("kind") != "agent":
        return ""
    snapshot = snapshot or {}
    overrides = snapshot.get("runnerOverrides") or {}
    if node_id in overrides:
        return str(overrides[node_id])
    inputs = (snapshot.get("values") or {}).get("inputs") or {}
    chosen = inputs.get(str(node.get("runnerInput") or ""))
    return str(chosen or node.get("runner") or "")


def timeline(
    topology: Mapping[str, Any] | None,
    events: Sequence[Mapping[str, Any]],
    snapshot: Mapping[str, Any] | None = None,
) -> list[Entry]:
    """Every node start in order, then the nodes not reached yet."""
    topology = topology or {"nodes": [], "edges": []}
    nodes = _node_map(topology)
    entries: list[Entry] = []
    by_execution: dict[str, Entry] = {}
    occurrences: dict[str, int] = {}
    for event in events:
        kind = event.get("type")
        node_id = str(event.get("nodeId") or "")
        execution = str(event.get("executionId") or "")
        if kind == "node.started" and node_id:
            occurrences[node_id] = occurrences.get(node_id, 0) + 1
            entry = _entry_for(
                nodes.get(node_id, {}), node_id, execution_id=execution, status=RUNNING,
                occurrence=occurrences[node_id], first_sequence=int(event.get("sequence") or 0),
            )
            entries.append(entry)
            if execution:
                by_execution[execution] = entry
            continue
        entry = by_execution.get(execution) if execution else None
        if entry is None and node_id:
            entry = next((one for one in reversed(entries) if one.node_id == node_id), None)
        if kind == "node.finished" and entry is not None:
            entry.status = DONE
        elif kind == "approval.requested" and entry is not None:
            payload = event.get("payload") or {}
            if not payload.get("autoApproved") and entry.status == RUNNING:
                entry.status = WAITING
        elif kind == "approval.resolved" and entry is not None and entry.status == WAITING:
            entry.status = RUNNING
        elif kind == "run.failed":
            for one in entries:
                if one.status in (RUNNING, WAITING):
                    one.status = FAILED if one.node_id == node_id or not node_id else STOPPED
        elif kind == "run.forked":
            for one in entries:
                if one.status in (RUNNING, WAITING):
                    one.status = STOPPED
        elif kind == "run.finished":
            for one in entries:
                if one.status in (RUNNING, WAITING):
                    one.status = DONE
    _reconcile(entries, snapshot)
    for entry in entries:
        entry.runner = resolved_runner(nodes.get(entry.node_id, {}), entry.node_id, snapshot)
    return entries + _upcoming(topology, entries, snapshot)


def _reconcile(entries: list[Entry], snapshot: Mapping[str, Any] | None) -> None:
    """The snapshot is the authority on what is working now."""
    if not snapshot:
        return
    active = {
        str(one.get("executionId")): str(one.get("nodeId"))
        for one in snapshot.get("activeExecutions") or []
    }
    waiting = {str(one.get("executionId")) for one in snapshot.get("pendingApprovals") or []}
    for entry in entries:
        if entry.execution_id in active:
            entry.status = WAITING if entry.execution_id in waiting else RUNNING
    if snapshot.get("status") in TERMINAL_RUN_STATUSES:
        for entry in entries:
            if entry.status in (RUNNING, WAITING):
                entry.status = DONE if snapshot.get("status") == "completed" else STOPPED


def _upcoming(
    topology: Mapping[str, Any],
    entries: Sequence[Entry],
    snapshot: Mapping[str, Any] | None,
) -> list[Entry]:
    nodes = _node_map(topology)
    order = node_order(topology)
    rank = {node_id: index for index, node_id in enumerate(order)}
    if snapshot and snapshot.get("status") in TERMINAL_RUN_STATUSES:
        return []
    if not entries:
        remaining = order
    else:
        frontier = [entry.node_id for entry in entries if entry.status in (RUNNING, WAITING)]
        frontier += [str(node) for node in (snapshot or {}).get("nextNodes") or []]
        if not frontier:
            frontier = [entries[-1].node_id]
        known = [rank[node] for node in frontier if node in rank]
        after = max(known) if known else len(order)
        # A node queued next has not started, so it is still to come.
        queued = {str(node) for node in (snapshot or {}).get("nextNodes") or []}
        started = {
            entry.node_id for entry in entries if entry.status in (RUNNING, WAITING)
        }
        remaining = [
            node for node in order
            if rank[node] > after or (node in queued and node not in started)
        ]
    return [
        _entry_for(
            nodes.get(node_id, {}), node_id, status=PENDING,
            runner=resolved_runner(nodes.get(node_id, {}), node_id, snapshot),
        )
        for node_id in remaining
    ]


def rows(
    entries: Sequence[Entry],
    *,
    runner_values: Mapping[str, str] | None = None,
) -> list[Row]:
    """Group consecutive entries that share a group under one header.

    `runner_values` is a creation form's current inputs: given, each row that
    one of them places says which runner, or which policy, will be used. A
    group whose members are all placed by the same input says it once, on its
    header.
    """
    result: list[Row] = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        if not entry.group:
            result.append(Row([entry], note=_note([entry], runner_values)))
            index += 1
            continue
        members = [entry]
        index += 1
        while (
            index < len(entries)
            and entries[index].group == entry.group
            and _same_round(members, entries[index])
        ):
            members.append(entries[index])
            index += 1
        shared = {member.runner_input for member in members}
        header_note = _note(members, runner_values) if len(shared) == 1 else ""
        result.append(Row(members, header=True, note=header_note))
        for member in members:
            result.append(Row(
                [member], indent=True,
                note="" if header_note else _note([member], runner_values),
            ))
    return result


def _same_round(members: Sequence[Entry], candidate: Entry) -> bool:
    """A group's second pass is a second block, not more of the first."""
    return all(member.node_id != candidate.node_id for member in members)


def _note(entries: Sequence[Entry], runner_values: Mapping[str, str] | None) -> str:
    entry = entries[0]
    if runner_values is not None:
        value = runner_values.get(entry.runner_input, "") if entry.runner_input else ""
        return runner_label(value) if value else ""
    return ""


# --- what a node said ----------------------------------------------------------


@dataclass
class Item:
    """One thing in a conversation: a message, a tool call, an approval."""

    kind: str  # "user", "assistant", "tool", "approval", "error", "system"
    text: str = ""
    status: str = ""
    diff: list[str] = field(default_factory=list)
    call_id: str = ""
    sequence: int = 0


def node_events(
    events: Sequence[Mapping[str, Any]], node_id: str,
) -> list[Mapping[str, Any]]:
    return [event for event in events if str(event.get("nodeId") or "") == node_id]


def conversation(events: Sequence[Mapping[str, Any]]) -> list[Item]:
    """A node's events as its conversation, the way the WorkOrder page reads them.

    A steering message is published twice -- when it is sent and again when
    the node takes it up -- and is shown once, where it was sent.
    """
    items: list[Item] = []
    calls: dict[str, Item] = {}
    steered: dict[str, int] = {}
    resolved = {
        str((event.get("payload") or {}).get("approvalId")): str(
            (event.get("payload") or {}).get("decision") or ""
        )
        for event in events if event.get("type") == "approval.resolved"
    }
    for event in events:
        payload = event.get("payload") or {}
        kind = event.get("type")
        sequence = int(event.get("sequence") or 0)
        if kind == "transcript":
            text = str(payload.get("text") or "")
            if not text:
                continue
            if str(payload.get("role") or "assistant") != "user":
                items.append(Item("assistant", text, sequence=sequence))
                continue
            if steered.get(text):
                steered[text] -= 1
                continue
            items.append(Item("user", text, sequence=sequence))
        elif kind == "steering.received":
            text = str(payload.get("message") or "")
            if text:
                steered[text] = steered.get(text, 0) + 1
                items.append(Item("user", text, sequence=sequence))
        elif kind == "tool.call":
            call_id = str(payload.get("callId") or "")
            arguments = payload.get("arguments") or {}
            name = str(payload.get("name") or "") or "tool"
            known = calls.get(call_id) if call_id else None
            if known is not None:
                known.text = name or known.text
                known.diff = known.diff or diff_lines(arguments)
                continue
            item = Item(
                "tool", name, status=str(arguments.get("status") or ""),
                diff=diff_lines(arguments), call_id=call_id, sequence=sequence,
            )
            items.append(item)
            if call_id:
                calls[call_id] = item
        elif kind == "tool.result":
            known = calls.get(str(payload.get("callId") or ""))
            if known is not None:
                known.status = str(payload.get("result") or known.status)
                if payload.get("name"):
                    known.text = str(payload["name"])
        elif kind == "approval.requested":
            if payload.get("autoApproved"):
                continue
            approval = str(payload.get("approvalId") or "")
            decision = resolved.get(approval)
            detail = str(payload.get("command") or payload.get("toolName") or "")
            reason = str(payload.get("reason") or "approval requested")
            items.append(Item(
                "approval", reason + (f": {detail}" if detail else ""),
                status=decision or "pending", sequence=sequence,
            ))
        elif kind == "run.failed":
            items.append(Item("error", str(payload.get("error") or "the run failed"), sequence=sequence))
        elif kind == "node.started" and items:
            items.append(Item("system", "started again", sequence=sequence))
    return items


def diff_lines(arguments: Mapping[str, Any], limit: int = 400) -> list[str]:
    """Unified diff lines for an edit an ACP tool call describes, if it is one."""
    changes: list[tuple[str, str, str]] = []
    for block in arguments.get("content") or []:
        if isinstance(block, Mapping) and block.get("type") == "diff":
            changes.append((
                str(block.get("path") or ""),
                str(block.get("oldText") or ""),
                str(block.get("newText") or ""),
            ))
    raw = arguments.get("rawInput")
    if not changes and isinstance(raw, Mapping) and "new_string" in raw:
        changes.append((
            str(raw.get("file_path") or raw.get("path") or ""),
            str(raw.get("old_string") or ""),
            str(raw.get("new_string") or ""),
        ))
    lines: list[str] = []
    for path, old, new in changes:
        body = list(difflib.unified_diff(
            old.splitlines(), new.splitlines(), lineterm="", n=1,
        ))[2:]
        body = [line for line in body if not line.startswith("@@")]
        added = sum(1 for line in body if line.startswith("+"))
        removed = sum(1 for line in body if line.startswith("-"))
        lines.append(f"@ {path}  +{added} -{removed}")
        lines.extend(body)
        if len(lines) >= limit:
            break
    return lines[:limit]


# --- findings --------------------------------------------------------------------


@dataclass
class Findings:
    title: str
    findings: list[Mapping[str, Any]]


def findings_for(
    row_entries: Sequence[Entry], values: Mapping[str, Any] | None,
) -> list[Findings]:
    """What a group's nodes found, read where each node says it keeps them.

    The node that ran last goes first: in a review that is the one
    consolidating everybody else's, and its survivors are what matters.
    """
    values = values or {}
    sections: list[Findings] = []
    for entry in reversed(row_entries):
        if not entry.findings_key:
            continue
        found = _finding_list(values.get(entry.findings_key))
        if found is not None:
            sections.append(Findings(entry.name, found))
    return sections


def _finding_list(value: object) -> list[Mapping[str, Any]] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
        if all("tagline" in item for item in value):
            return list(value)
    return None


def pending_approvals(
    snapshot: Mapping[str, Any] | None, node_id: str,
) -> list[Mapping[str, Any]]:
    return [
        approval for approval in (snapshot or {}).get("pendingApprovals") or []
        if str(approval.get("nodeId")) == node_id
    ]


def is_active(snapshot: Mapping[str, Any] | None, node_id: str) -> bool:
    return any(
        str(one.get("nodeId")) == node_id
        for one in (snapshot or {}).get("activeExecutions") or []
    )


def can_write(
    topology: Mapping[str, Any] | None, snapshot: Mapping[str, Any] | None, node_id: str,
) -> bool:
    """Whether a message to this node would be accepted.

    A working node takes steering; an always-open node (implementation) takes
    it even after the run has moved on, by being sent back there to carry on --
    which is what renews it. Anything else, the reviewers included, has
    nothing to say it to once it has finished.
    """
    if is_active(snapshot, node_id) or pending_approvals(snapshot, node_id):
        return True
    node = _node_map(topology or {}).get(node_id) or {}
    return bool(node.get("alwaysOpen")) and bool(snapshot)


__all__ = [
    "Entry",
    "Findings",
    "Item",
    "Row",
    "can_write",
    "conversation",
    "diff_lines",
    "findings_for",
    "node_events",
    "node_order",
    "pending_approvals",
    "rows",
    "runner_label",
    "timeline",
]

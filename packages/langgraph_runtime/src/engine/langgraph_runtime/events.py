"""What a run has raised, in order, and how a subscriber catches up.

Everything a client is told about a run arrives here first. The log is
append-only and every entry carries a `sequence`, so "subscribe to workflow
events" is a replay from a cursor followed by a live tail rather than a live
tail alone: a browser that reconnects asks for what it has not seen instead of
losing the approval request it was about to show.

The log is the control surface's, not the graph's. A LangGraph binding publishes
into it through one callback and never has to think about who is listening, how
many of them there are, or which of them fell over.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum

from engine.domain import RunId

from engine.langgraph_runtime.topology import NodeId


class EventKind(Enum):
    """Every kind of thing a subscriber can be told.

    The values are the wire spelling, so a client switches on the same strings
    this module is written in.
    """

    RUN_STARTED = "run.started"
    NODE_STARTED = "node.started"
    NODE_FINISHED = "node.finished"
    TRANSCRIPT = "transcript"
    """One message in a node's conversation: `role` and `text`."""
    TOOL_CALL = "tool.call"
    """A call the node made: `callId`, `name`, `arguments`."""
    TOOL_RESULT = "tool.result"
    """What that call returned: `callId`, `result`."""
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    STEERING_RECEIVED = "steering.received"
    """A message was delivered to the node currently running."""
    TRANSITION = "transition"
    """Control was moved by hand rather than by the graph: `from` and `to`."""
    STATE_UPDATED = "state.updated"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    """One thing that happened, addressed to one run.

    `sequence` is assigned by the log rather than by whoever raised the event,
    and is 0 until then: a producer has no way to know what it should be, and a
    subscriber's cursor would be meaningless if two producers guessed.
    """

    run_id: RunId
    kind: EventKind
    payload: Mapping[str, object] = field(default_factory=dict)
    node_id: NodeId | None = None
    sequence: int = 0


EventObserver = Callable[[RuntimeEvent], Awaitable[None]]
"""Where a graph publishes. `EventLog.append` is the one the server installs."""


class EventLog:
    """Every event each run has raised, replayable from any cursor."""

    def __init__(self) -> None:
        self._events: dict[RunId, list[RuntimeEvent]] = {}
        self._changed: dict[RunId, asyncio.Condition] = {}

    async def append(self, event: RuntimeEvent) -> RuntimeEvent:
        """Record one event and wake everyone watching its run."""
        condition = self._condition(event.run_id)
        async with condition:
            recorded = self._events.setdefault(event.run_id, [])
            numbered = replace(event, sequence=len(recorded) + 1)
            recorded.append(numbered)
            condition.notify_all()
        return numbered

    def since(self, run_id: RunId, cursor: int = 0) -> tuple[RuntimeEvent, ...]:
        """Everything raised for this run after `cursor`."""
        return tuple(
            event for event in self._events.get(run_id, ()) if event.sequence > cursor
        )

    async def stream(
        self, run_id: RunId, cursor: int = 0
    ) -> AsyncIterator[RuntimeEvent]:
        """Replay from `cursor`, then follow the run for as long as anyone reads.

        Never ends on its own, not even once the run has finished: a finished
        run can be sent back to an earlier node by hand, and a subscription that
        closed itself on `run.finished` would miss the run starting again.
        Subscribers stop by stopping.
        """
        condition = self._condition(run_id)
        while True:
            pending = self.since(run_id, cursor)
            for event in pending:
                cursor = event.sequence
                yield event
            async with condition:
                await condition.wait_for(lambda: bool(self.since(run_id, cursor)))

    def _condition(self, run_id: RunId) -> asyncio.Condition:
        return self._changed.setdefault(run_id, asyncio.Condition())


__all__ = ["EventKind", "EventLog", "EventObserver", "RuntimeEvent"]

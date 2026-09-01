"""What a run has raised, in order, and how a subscriber catches up.

Everything a client is told about a run arrives here first. The log is
append-only and every entry carries a `sequence`, so "subscribe to workflow
events" is a replay from a cursor followed by a live tail rather than a live
tail alone: a browser that reconnects asks for what it has not seen instead of
losing the approval request it was about to show.

The log is the control surface's, not the graph's. A binding publishes into it
through one callback and never has to think about who is listening, how many of
them there are, or which of them fell over. That includes the events a node's
own execution raises -- an ACP session's tool calls, transcript and permission
requests are translated into this vocabulary by the node that owns the session,
so a client watching a run does not need to know one is involved.

It is process-local and unbounded, which is a decision and not an oversight, but
only for as long as the graph behind it is. `apps/web`'s `ApprovalFeed` is the
shape this ends up: persistence is the source of truth and the condition is only
a wake-up signal, so a reconnect cannot lose an event and nothing has to be kept
in memory to make replay work. That needs a store to write to, and which store
is a question for the binding rather than for the mock -- so the eviction hook
is deliberately absent rather than guessed at, and every run a process has
handled is replayable until it restarts.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum

from engine.domain import RunId

from engine.graph_runtime.topology import NodeId


class EventKind(Enum):
    """Every kind of thing a subscriber can be told.

    The values are the wire spelling, so a client switches on the same strings
    this module is written in.
    """

    RUN_STARTED = "run.started"
    CHECKPOINT = "checkpoint"
    """A superstep boundary was saved: `checkpointId`, `parentId`, `nextNodes`.

    How a subscriber follows a run's position without polling, and how it learns
    the ids a resume can name.
    """
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
    """A message was routed to an execution that was already running."""
    RUN_FORKED = "run.forked"
    """A resume was asked for: `from`, `checkpointId`, `nodes`.

    Named for what it does. Nothing is rewritten or removed -- the checkpoint it
    came from keeps its other children, so the attempt being replaced stays
    readable beside the one replacing it.
    """
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
        """Everything raised for this run after `cursor`.

        A slice rather than a scan. `append` numbers events densely from 1 and
        never reorders them, so the cursor is the index of the next one -- and
        the difference matters: `stream` asks this question once per event it
        delivers, per subscriber, so a filter would make one long-lived agent
        node quadratic in the number of events it raised.
        """
        return tuple(self._events.get(run_id, ())[cursor:])

    async def stream(
        self, run_id: RunId, cursor: int = 0
    ) -> AsyncIterator[RuntimeEvent]:
        """Replay from `cursor`, then follow the run for as long as anyone reads.

        Never ends on its own, not even once the run has finished: a finished
        run can be resumed from an earlier checkpoint, and a subscription that
        closed itself on `run.finished` would miss the run starting again.
        Subscribers stop by stopping.

        Each subscriber has its own cursor and its own generator, so two of them
        reading the same run at different points cannot advance each other.
        """
        condition = self._condition(run_id)
        while True:
            for event in self.since(run_id, cursor):
                cursor = event.sequence
                yield event
            async with condition:
                # Length rather than `since`, because `Condition.wait_for` calls
                # this on every `notify_all` and holds the lock `append` needs.
                await condition.wait_for(
                    lambda: len(self._events.get(run_id, ())) > cursor
                )

    def _condition(self, run_id: RunId) -> asyncio.Condition:
        return self._changed.setdefault(run_id, asyncio.Condition())


__all__ = ["EventKind", "EventLog", "EventObserver", "RuntimeEvent"]

"""Publish committed public events from the transactional outbox.

`Transaction.append_event` stores an event in the same transaction as the state
that caused it; `OutboxPublisher` later publishes it outside any transaction and
marks it published only after the broker acknowledges it. A crash, cancellation
or lost acknowledgement leaves the record pending, so it is published again with
the same event ID and envelope: delivery is at least once. The broker's
`Nats-Msg-Id` deduplication covers only its short window, so consumers must
still deduplicate on `event_id`.

Events of one workspace are published in insertion order: once one fails, its
workspace's later events wait for the next pass, and the pass reads past them to
reach other workspaces. Nothing here knows event
types, so task and conversation producers can append through the same port.
This assumes the service's single-writer deployment; it does not coordinate
several publishers.
"""

from __future__ import annotations

import asyncio
import logging

from virtual_control_plane.application.ports import EventPublisher
from virtual_control_plane.application.transactions import Outbox, PendingEvent as PendingEvent

log = logging.getLogger(__name__)


class PublishFailed(RuntimeError):
    """At least one pending event was not acknowledged; it stays pending."""

    def __init__(self, workspaces: int) -> None:
        super().__init__(f"{workspaces} workspace(s) have unacknowledged events")


class OutboxPublisher:
    def __init__(
        self, outbox: Outbox, publisher: EventPublisher, *, batch_size: int = 100,
        idle_interval: float = 1.0, min_backoff: float = 0.1, max_backoff: float = 10.0,
    ) -> None:
        self._outbox, self._publisher = outbox, publisher
        self._batch_size, self._idle_interval = batch_size, idle_interval
        self._min_backoff, self._max_backoff = min_backoff, max_backoff
        self._wake = asyncio.Event()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def notify(self) -> None:
        """Publish promptly after a commit appended events, rather than at the next poll."""
        self._wake.set()

    async def publish_pending(self) -> int:
        """Publish the head of every workspace; return how many were acknowledged, or raise `PublishFailed`."""
        blocked: set[str] = set()
        published = 0
        while not self._stopping.is_set():
            # Refetch without newly blocked workspaces, so their backlog cannot hide everyone else's.
            events = await self._outbox.pending_events(self._batch_size, exclude=blocked)
            newly_blocked = False
            for event in events:
                if self._stopping.is_set():
                    break
                if event.workspace_id in blocked:
                    continue
                try:
                    await self._publisher.publish(event.envelope)
                except Exception:
                    # A later event of this workspace must not overtake this one.
                    blocked.add(event.workspace_id)
                    newly_blocked = True
                    log.warning("event not acknowledged; it stays pending",
                                extra={"vcp.event_id": event.event_id, "vcp.outbox_sequence": event.sequence},
                                exc_info=True)
                    continue
                await self._outbox.mark_published(event.sequence)
                published += 1
            if len(events) < self._batch_size or not newly_blocked:
                break
        if blocked:
            raise PublishFailed(len(blocked))
        return published

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="vcp-outbox")

    async def close(self, timeout: float = 5.0) -> None:
        """Stop between events; cancel an in-flight publish after `timeout`. Unfinished events stay pending."""
        self._stopping.set()
        if self._task is None:
            return
        done, _ = await asyncio.wait([self._task], timeout=timeout)
        if not done:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        backoff = self._min_backoff
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                published = await self.publish_pending()
            except Exception:
                # Covers broker outages and database failures alike: nothing is lost.
                log.warning("outbox publication failed; retrying in %.1fs", backoff, exc_info=True)
                await self._pause(backoff, wakeable=False)
                backoff = min(backoff * 2, self._max_backoff)
                continue
            backoff = self._min_backoff
            if published < self._batch_size:
                await self._pause(self._idle_interval, wakeable=True)

    async def _pause(self, seconds: float, *, wakeable: bool) -> None:
        waits = [asyncio.ensure_future(self._stopping.wait())]
        if wakeable:
            waits.append(asyncio.ensure_future(self._wake.wait()))
        try:
            await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()

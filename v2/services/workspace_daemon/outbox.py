"""Send the oldest unacknowledged journal update until VCP commits it.

Bytes go out exactly as journaled, on every retry and after restart. Only a
verified `committed` acknowledgement advances; an `EVENT_GAP` reopens retained
updates from the requested sequence; any other permanent rejection blocks the
outbox (and new execution) for operator reconciliation instead of skipping it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Protocol

from openengine.contracts.runtime import validate_exchange, validate_wire_message
from openengine.messaging import ServiceError
from openengine.messaging.retry import Backoff

log = logging.getLogger(__name__)


class Transport(Protocol):
    async def send(self, subject: str, wire: bytes) -> bytes: ...


class UpdateOutbox:
    def __init__(self, journal, transport: Transport, poll_seconds: float = 0.05) -> None:
        self.journal, self.transport, self.poll_seconds = journal, transport, poll_seconds
        self.backoff = Backoff()
        self._worker: asyncio.Task | None = None

    async def run_once(self) -> bool:
        """Deliver one update; True when VCP committed it."""
        store, subject = self.journal.store, self.journal.updates
        async with store.read() as tx:
            meta = await tx.get("meta", "runtime")
            pending = None if meta.get("blocked") else await tx.oldest_pending()
        if pending is None:
            return False
        sequence, wire = pending
        key = str(sequence)
        if not self.backoff.due(key):
            return False
        try:
            reply = await self.transport.send(subject, wire)
        except ServiceError:
            self.backoff.failed(key)
            return False
        errors = validate_wire_message("update_ack", reply, subject)
        ack = {} if errors else json.loads(reply)
        if errors or validate_exchange("update", json.loads(wire), ack, subject):
            log.warning("unverifiable update acknowledgement", extra={"daemon.sequence": sequence})
            self.backoff.failed(key)
            return False
        async with store.transaction() as tx:
            meta = await tx.get("meta", "runtime")
            if ack["status"] == "committed":
                await tx.acknowledge(sequence)
                if sequence == meta.get("ready_sequence"):
                    meta["ready"] = True  # VCP committed readiness: admission may begin.
            elif ack["error"]["code"] == "EVENT_GAP" and await tx.has_update(ack["expected_sequence"]):
                await tx.replay_from(ack["expected_sequence"])
            elif not ack["error"]["retryable"] or ack["error"]["code"] == "EVENT_GAP":
                meta.update(ready=False, blocked={"code": ack["error"]["code"], "sequence": sequence,
                                                  "event_id": ack["event_id"]})
                log.error("update rejected; runtime blocked for reconciliation",
                          extra={f"daemon.{k}": v for k, v in meta["blocked"].items()})
            await tx.put("meta", "runtime", meta)
        if ack["status"] == "committed":
            self.backoff.done(key)
            return True
        self.backoff.failed(key)
        return False

    def start(self) -> None:
        self._worker = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                while await self.run_once():
                    pass
            except Exception:
                log.exception("update delivery failed; retrying")
            await asyncio.sleep(self.poll_seconds)

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

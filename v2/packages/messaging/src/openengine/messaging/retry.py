"""Per-key capped exponential backoff with jitter, for durable delivery workers.

A worker that keeps its records durably retries them for as long as they are
unacknowledged; this only spaces those retries. Timers live in memory, so a
restart forgets them, never the records themselves.
"""

from __future__ import annotations

import random
import time


class Backoff:
    def __init__(self, initial: float = 0.25, maximum: float = 10.0) -> None:
        self.initial, self.maximum = initial, maximum
        self._pending: dict[str, tuple[int, float]] = {}

    def due(self, key: str) -> bool:
        return self._pending.get(key, (0, 0.0))[1] <= time.monotonic()

    def delay(self, failures: int) -> float:
        """The longest wait after `failures` consecutive failures."""
        return min(self.maximum, self.initial * 2 ** min(failures - 1, 32))

    def failed(self, key: str) -> None:
        count = self._pending.get(key, (0, 0.0))[0] + 1
        delay = self.delay(count)
        self._pending[key] = (count, time.monotonic() + random.uniform(delay / 2, delay))

    def done(self, key: str) -> None:
        self._pending.pop(key, None)

"""Turn SIGTERM and SIGINT into a shutdown request the supervisor polls."""

from __future__ import annotations

import signal
import threading


class ShutdownRequest:
    def __init__(self) -> None:
        self._event = threading.Event()

    def install(self) -> None:
        # SIGINT is set explicitly because a process started in the background
        # inherits it ignored.
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self._request)

    def _request(self, signum: int, frame: object) -> None:
        self._event.set()

    def requested(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)

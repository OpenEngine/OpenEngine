"""Own one internal nats-server until VCP's control pipe closes.

Run as `python -I broker_process.py LOCK_FD BINARY CONFIG`, standard library
only. VCP holds the only write end of this process's stdin: each `R` byte asks
for a configuration reload (SIGHUP to the server this process started), and
EOF -- including after VCP is SIGKILLed -- terminates the server, escalating to
SIGKILL after a deadline, before the inherited `broker.lock` is released. The
only PID ever signalled is the live child started here, never one from disk.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys

TERMINATE_SECONDS = 3.0


def supervise(lock_fd: int, binary: str, config: str) -> int:
    # SIGTERM to this process still cleans up its server.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = None
    try:
        server = subprocess.Popen([binary, "-c", config], stdin=subprocess.DEVNULL)
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
            while server.poll() is None:
                if not selector.select(0.1):
                    continue
                control = os.read(sys.stdin.fileno(), 64)
                if not control:
                    return 0
                if b"R" in control and server.poll() is None:
                    server.send_signal(signal.SIGHUP)
        return server.returncode or 1
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=TERMINATE_SECONDS)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()
        # The lock spans spawn through reaping: no replacement can start early.
        os.close(lock_fd)


if __name__ == "__main__":
    sys.exit(supervise(int(sys.argv[1]), sys.argv[2], sys.argv[3]))

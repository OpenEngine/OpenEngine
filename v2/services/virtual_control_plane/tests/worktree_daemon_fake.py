"""A stand-in for `python -m workspace_daemon` that only does what supervision observes.

It takes the journal's exclusive lock exactly as the daemon does, appends its
arguments to `{journal}/starts.jsonl`, writes the ready file and waits. With
`OE_TEST_DAEMON_IGNORE_TERM=1` it ignores SIGTERM, so only SIGKILL stops it.
No broker is involved.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path


def main(arguments: list[str]) -> int:
    options = dict(zip(arguments, arguments[1:], strict=False))
    journal, ready = Path(options["--journal-directory"]), Path(options["--ready-file"])
    lock = os.open(journal / ".daemon.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 2
    with (journal / "starts.jsonl").open("a") as starts:
        starts.write(json.dumps(arguments) + "\n")
    if os.environ.get("OE_TEST_DAEMON_IGNORE_TERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ready.write_text("listening")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

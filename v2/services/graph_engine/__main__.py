"""Run the Graph Engine (ge).

It has no handlers yet: it boots, logs what it would serve with, and waits
for the supervisor to stop it.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading

from openengine.persistence import DatabaseConfig
from openengine.telemetry import configure

from graph_engine.persistence import PERSISTENCE

log = logging.getLogger("graph_engine")


def main() -> int:
    telemetry = configure("ge")
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    with telemetry.tracer(__name__).start_as_current_span("ge boot"):
        log.info("starting")
        config = DatabaseConfig.from_environment()
        database = PERSISTENCE.database(config)
        log.info(
            "database configured",
            extra={"db.system.name": config.backend.value, "db.namespace": PERSISTENCE.name,
                   "db.url": database.url(asynchronous=False).render_as_string(hide_password=True)},
        )
        log.info("nats configured", extra={"server.address": os.environ.get("OE_NATS_URL", "unset")})
        log.info("ready")
    stopped.wait()
    log.info("stopping")
    telemetry.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

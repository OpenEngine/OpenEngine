"""Start OpenEngine V2's local processes, wait for them, and keep them running.

NATS first, then the services, which are stopped before it. The services
have no handlers yet; they boot, log, and wait.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from openengine.supervisor.processes import nats, services
from openengine.supervisor.readiness.http import NotReady, wait_until_healthy
from openengine.supervisor.shutdown.processes import stop_all
from openengine.supervisor.shutdown.signals import ShutdownRequest
from openengine.supervisor.state import ProcessState, Status, SupervisorState, state_path, write_state

READY_TIMEOUT_SECONDS = 30.0
WATCH_SECONDS = 0.5
EXIT_OK = 0
EXIT_FAILED = 1

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    state_directory: Path
    cache_directory: Path
    host: str
    nats_port: int
    nats_monitor_port: int
    nats_server: Path | None = None


class ProcessExited(RuntimeError):
    def __init__(self, name: str, code: int) -> None:
        super().__init__(f"{name} exited with status {code}")


def _watch(children: dict[str, subprocess.Popen], shutdown: ShutdownRequest) -> None:
    """Block until shutdown is requested; raise if a child exits first."""
    while not shutdown.wait(WATCH_SECONDS):
        for name, process in children.items():
            if (code := process.poll()) is not None:
                raise ProcessExited(name, code)


def run(settings: Settings) -> int:
    shutdown = ShutdownRequest()
    shutdown.install()
    path = state_path(settings.state_directory)
    pid = os.getpid()
    write_state(path, SupervisorState(pid=pid, status=Status.STARTING))
    children: dict[str, subprocess.Popen] = {}
    try:
        binary = settings.nats_server or nats.install_nats_server(settings.cache_directory)
        store = settings.state_directory / "nats"
        store.mkdir(parents=True, exist_ok=True)
        command = nats.server_command(
            binary,
            host=settings.host,
            port=settings.nats_port,
            monitor_port=settings.nats_monitor_port,
            store=store,
        )
        children["nats"] = nats.start(command, sys.stdout.buffer)
        wait_until_healthy(
            "nats-server",
            children["nats"],
            f"http://{settings.host}:{settings.nats_monitor_port}/healthz",
            timeout=READY_TIMEOUT_SECONDS,
            cancelled=shutdown.requested,
        )
        nats_url = f"nats://{settings.host}:{settings.nats_port}"
        processes = [ProcessState("nats", children["nats"].pid, nats_url)]
        for name, module in services.SERVICES:
            children[name] = services.start(module, sys.stdout.buffer, nats_url=nats_url)
            processes.append(ProcessState(name, children[name].pid, ""))
        write_state(path, SupervisorState(pid=pid, status=Status.READY, processes=processes))
        log.info("ready, NATS on %s", nats_url, extra={"services": [name for name, _ in services.SERVICES]})
        _watch(children, shutdown)
    except (nats.NatsError, NotReady, ProcessExited) as error:
        log.exception("failed; stopping")
        write_state(path, SupervisorState(pid=pid, status=Status.FAILED, error=str(error)))
        return EXIT_FAILED
    finally:
        stop_all(list(children.values()))
    path.unlink(missing_ok=True)
    log.info("stopped")
    return EXIT_OK

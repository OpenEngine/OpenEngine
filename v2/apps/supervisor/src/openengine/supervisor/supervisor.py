"""Start OpenEngine V2's local processes, wait for them, and keep them running.

NATS first, then the services, which are stopped before it. A service with
a readiness check (ge and, when enabled, vcp) names its health URL in a ready
file; the supervisor is READY only once that URL answers 200. If it never
does, the supervisor writes FAILED with the reason and stops everything. vcp
is opt-in: `Settings.vcp_enabled` decides whether it starts at all.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from openengine.supervisor.processes import nats, services
from openengine.supervisor.readiness.http import NotReady, wait_for_ready_file, wait_until_healthy
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
    vcp_enabled: bool = False


class ProcessExited(RuntimeError):
    def __init__(self, name: str, code: int) -> None:
        super().__init__(f"{name} exited with status {code}")


def _watch(children: dict[str, subprocess.Popen], shutdown: ShutdownRequest) -> None:
    """Block until shutdown is requested; raise if a child exits first."""
    while not shutdown.wait(WATCH_SECONDS):
        for name, process in children.items():
            if (code := process.poll()) is not None:
                raise ProcessExited(name, code)


def _wait_until_ready(
    name: str, process: subprocess.Popen, ready_file: Path, shutdown: ShutdownRequest
) -> ProcessState:
    url = wait_for_ready_file(name, process, ready_file, timeout=READY_TIMEOUT_SECONDS, cancelled=shutdown.requested)
    wait_until_healthy(name, process, url, timeout=READY_TIMEOUT_SECONDS, cancelled=shutdown.requested)
    return ProcessState(name, process.pid, url, Status.READY)


def run(settings: Settings) -> int:
    shutdown = ShutdownRequest()
    shutdown.install()
    path = state_path(settings.state_directory)
    pid = os.getpid()
    write_state(path, SupervisorState(pid=pid, status=Status.STARTING))
    children: dict[str, subprocess.Popen] = {}
    processes: list[ProcessState] = []
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
        processes.append(ProcessState("nats", children["nats"].pid, nats_url, Status.READY))
        started = services.enabled(vcp=settings.vcp_enabled)
        ready_files: dict[str, Path] = {}
        for name, module in started:
            environment = {}
            if variable := services.READY_FILE_VARIABLES.get(name):
                ready_files[name] = services.ready_file(settings.state_directory, name)
                ready_files[name].unlink(missing_ok=True)
                environment[variable] = str(ready_files[name])
            if variable := services.STATE_DIR_VARIABLES.get(name):
                state_dir = settings.state_directory / name
                state_dir.mkdir(parents=True, exist_ok=True)
                environment[variable] = str(state_dir)
            children[name] = services.start(module, sys.stdout.buffer, nats_url=nats_url, environment=environment)
            processes.append(ProcessState(name, children[name].pid, ""))
        for index, process in enumerate(processes):
            if ready_file := ready_files.get(process.name):
                try:
                    processes[index] = _wait_until_ready(process.name, children[process.name], ready_file, shutdown)
                except NotReady:
                    processes[index] = replace(process, status=Status.FAILED)
                    raise
        write_state(path, SupervisorState(pid=pid, status=Status.READY, processes=processes))
        log.info("ready, NATS on %s", nats_url, extra={"services": [name for name, _ in started]})
        _watch(children, shutdown)
    except (nats.NatsError, NotReady, ProcessExited) as error:
        log.exception("failed; stopping")
        write_state(path, SupervisorState(pid=pid, status=Status.FAILED, processes=processes, error=str(error)))
        return EXIT_FAILED
    finally:
        stop_all(list(children.values()))
    path.unlink(missing_ok=True)
    log.info("stopped")
    return EXIT_OK

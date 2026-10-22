"""Start OpenEngine V2's local processes, wait for them, and keep them running.

For now that is NATS alone. The services join `run` as they are written,
started after NATS and stopped before it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from openengine.supervisor.processes import nats
from openengine.supervisor.readiness.http import NotReady, wait_until_healthy
from openengine.supervisor.shutdown.processes import stop_all
from openengine.supervisor.shutdown.signals import ShutdownRequest
from openengine.supervisor.state import ProcessState, Status, SupervisorState, state_path, write_state

READY_TIMEOUT_SECONDS = 30.0
WATCH_SECONDS = 0.5
EXIT_OK = 0
EXIT_FAILED = 1


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
        processes = [ProcessState("nats", children["nats"].pid, f"nats://{settings.host}:{settings.nats_port}")]
        if os.environ.get("OE_VCP_ENABLED") == "1":
            processes.append(_start_vcp(settings, children, shutdown))
        write_state(path, SupervisorState(pid=pid, status=Status.READY, processes=processes))
        print(f"supervisor: ready, NATS on nats://{settings.host}:{settings.nats_port}", flush=True)
        _watch(children, shutdown)
    except (nats.NatsError, NotReady, ProcessExited, RuntimeError, OSError) as error:
        print(f"supervisor: {error}", flush=True)
        write_state(path, SupervisorState(pid=pid, status=Status.FAILED, error=str(error)))
        return EXIT_FAILED
    finally:
        stop_all(list(children.values()))
    path.unlink(missing_ok=True)
    print("supervisor: stopped", flush=True)
    return EXIT_OK


def _start_vcp(settings, children, shutdown):
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        message = "Local VCP requires a loopback NATS listener"
        raise RuntimeError(message)
    ready = settings.state_directory / "vcp.ready"
    ready.unlink(missing_ok=True)
    children["vcp"] = subprocess.Popen(
        [sys.executable, "-m", "virtual_control_plane.service", "--local-account",
         "--nats-url", f"nats://{settings.host}:{settings.nats_port}", "--ready-file", str(ready)],
        stdin=subprocess.DEVNULL, stdout=sys.stdout.buffer, stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while not ready.exists():
        if (code := children["vcp"].poll()) is not None:
            raise ProcessExited("vcp", code)
        if shutdown.requested() or time.monotonic() >= deadline:
            message = "VCP did not become ready"
            raise RuntimeError(message)
        time.sleep(0.05)
    health_url = ready.read_text()
    wait_until_healthy("vcp", children["vcp"], health_url,
                       timeout=READY_TIMEOUT_SECONDS, cancelled=shutdown.requested)
    return ProcessState("vcp", children["vcp"].pid, health_url)

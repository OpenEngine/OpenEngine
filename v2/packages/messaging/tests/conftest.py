"""An operator-mode nats-server for the live tests, started from a generated deployment."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import nkeys
import pytest

from openengine.messaging import operator
from openengine.supervisor.processes.nats import install_nats_server

STARTUP_SECONDS = 30.0


def _port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass(frozen=True)
class OperatorBroker:
    """A running operator-mode nats-server and the deployment it was provisioned from."""

    url: str
    monitor: str
    """The monitoring endpoint, `http://…`."""
    deployment: operator.Deployment
    written: operator.Written
    log: Path
    tenant: operator.Identity
    """A test-only tenant account importing the service account's subjects with its own key."""
    forger: operator.Identity
    """A test-only account whose imports name the tenant's key instead of its own."""

    def creds(self, user: str) -> str:
        return str(self.written.creds[user])


def provision_with_test_tenants(directory: Path) -> tuple[operator.Deployment, operator.Written, operator.Identity, operator.Identity]:
    deployment = operator.Deployment.generate()
    tenant_keys = operator.generate_keys(nkeys.PREFIX_BYTE_ACCOUNT)
    tenant_key = operator.public_key(tenant_keys)
    imports = operator.tenant_imports(deployment.service.public_key, tenant_key)
    tenant = deployment.account("tenant", tenant_keys, imports=imports)
    forger = deployment.account("forger", operator.generate_keys(nkeys.PREFIX_BYTE_ACCOUNT), imports=imports)
    deployment.user(tenant, "tenant-user")
    deployment.user(forger, "forger-user")
    return deployment, deployment.write(directory), tenant, forger


def wait_until_healthy(process: subprocess.Popen, url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert process.poll() is None, "nats-server exited before it was healthy"
        try:
            with urlopen(url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(0.05)
    pytest.fail(f"nats-server did not answer {url} within {timeout}s")


@pytest.fixture(scope="module")
def nats_binary() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "openengine"
    return install_nats_server(cache)


@pytest.fixture(scope="module")
def operator_broker(nats_binary: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[OperatorBroker]:
    """Boot nats-server from a generated operator-mode config and wait on /healthz."""
    directory = tmp_path_factory.mktemp("operator")
    deployment, written, tenant, forger = provision_with_test_tenants(directory)
    port, monitor_port = _port(), _port()
    log = directory / "nats-server.log"
    command = [
        str(nats_binary), "--config", str(written.config), "--addr", "127.0.0.1", "--port", str(port),
        "--http_port", str(monitor_port), "--store_dir", str(directory / "jetstream"), "-D",
    ]
    with log.open("wb") as handle:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT)
    try:
        wait_until_healthy(process, f"http://127.0.0.1:{monitor_port}/healthz", STARTUP_SECONDS)
        yield OperatorBroker(f"nats://127.0.0.1:{port}", f"http://127.0.0.1:{monitor_port}", deployment, written, log, tenant, forger)
    finally:
        process.terminate()
        process.wait(timeout=10)

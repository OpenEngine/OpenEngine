"""Fetch and start the nats-server the supervisor runs.

The binary is not bundled: the first start downloads the pinned release archive
for this platform from GitHub, checks it against the checksum recorded here,
and caches the extracted binary per version. Later starts use the cache.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import platform
import socket
import subprocess
import tarfile
import tempfile
from importlib.resources import as_file, files
from pathlib import Path
from typing import IO
from urllib.error import URLError
from urllib.request import urlopen

NATS_VERSION = "2.15.1"
RELEASE_URL = f"https://github.com/nats-io/nats-server/releases/download/v{NATS_VERSION}"
# From the release's SHA256SUMS. Bumping NATS_VERSION means replacing these.
ARCHIVE_SHA256 = {
    ("darwin", "amd64"): "3cba5976a4fbd022669d3401cab4147faeb2b4c079712c882edb9ae4e52feb06",
    ("darwin", "arm64"): "ed0fe39222ec2e317c4d0e1a9cdc16c63ec523d94f9355d24a902a4db2655ffd",
    ("linux", "amd64"): "bf5edc7b1ce98e885c368cb43a886eb148dc5d0551d99f3cf6141fb5e1954c70",
    ("linux", "arm64"): "e2fbab4cad7d3c76e3052eb959570393c9d73ba756b70bf8e9eea61e9ae6d49c",
}
MACHINES = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
DOWNLOAD_TIMEOUT_SECONDS = 120.0


class NatsError(RuntimeError):
    pass


class UnsupportedPlatform(NatsError):
    def __init__(self, system: str, machine: str) -> None:
        super().__init__(f"no nats-server {NATS_VERSION} build is pinned for {system}-{machine}")


class DownloadFailed(NatsError):
    def __init__(self, url: str, error: Exception) -> None:
        super().__init__(f"could not download {url}: {error}")


class ChecksumMismatch(NatsError):
    def __init__(self, url: str, actual: str, expected: str) -> None:
        super().__init__(f"{url} has sha256 {actual}, expected {expected}")


class MissingBinary(NatsError):
    def __init__(self, url: str, member: str) -> None:
        super().__init__(f"{url} does not contain {member}")


class PortInUse(NatsError):
    def __init__(self, host: str, port: int, role: str) -> None:
        super().__init__(
            f"{host}:{port} (the {role}) is already in use, perhaps by another OpenEngine; stop it with `oe down`"
            " or choose other ports with `oe up --nats-port` and `--nats-monitor-port`"
        )


def current_platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = MACHINES.get(platform.machine().lower(), platform.machine().lower())
    return system, machine


def install_nats_server(
    cache: Path,
    *,
    target: tuple[str, str] | None = None,
    release_url: str = RELEASE_URL,
    checksums: dict[tuple[str, str], str] = ARCHIVE_SHA256,
) -> Path:
    """The cached nats-server binary, downloading and verifying it first if absent."""
    binary = cache / "nats-server" / NATS_VERSION / "nats-server"
    if binary.is_file():
        return binary
    system, machine = target or current_platform()
    expected = checksums.get((system, machine))
    if expected is None:
        raise UnsupportedPlatform(system, machine)
    stem = f"nats-server-v{NATS_VERSION}-{system}-{machine}"
    url = f"{release_url}/{stem}.tar.gz"
    try:
        with urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            archive = response.read()
    except (URLError, OSError) as error:
        raise DownloadFailed(url, error) from error
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected:
        raise ChecksumMismatch(url, actual, expected)
    member = f"{stem}/nats-server"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        extracted = tar.extractfile(member)
        if extracted is None:
            raise MissingBinary(url, member)
        content = extracted.read()
    # Write beside the target and rename, so an interrupted install never
    # leaves a partial binary that a later start would trust.
    binary.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=binary.parent, delete=False) as partial:
        partial.write(content)
    os.chmod(partial.name, 0o755)
    os.replace(partial.name, binary)
    return binary


def nats_config() -> Path:
    """The packaged infra/nats/nats-server.conf."""
    with as_file(files("openengine.supervisor.processes") / "nats-server.conf") as path:
        return path


def ensure_ports_free(host: str, *, port: int, monitor_port: int) -> None:
    """Raise PortInUse rather than start a nats-server that cannot bind `port` or `monitor_port`.

    nats-server exits when a port is taken, but a stranger's nats-server on the
    monitoring port answers the health check in its place. The probe binds as
    nats-server does, with SO_REUSEADDR, so connections a stopped server left in
    TIME_WAIT do not count as in use.
    """
    for role, number in (("NATS client port", port), ("NATS monitoring port", monitor_port)):
        try:
            socket.create_server((host, number)).close()
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            raise PortInUse(host, number, role) from None


def ports_file(directory: Path, binary: Path, pid: int) -> Path:
    """Where a nats-server from `binary`, given `--ports_file_dir directory`, records its addresses once it listens."""
    return directory / f"{binary.name}_{pid}.ports"


def remove_stale_ports_files(directory: Path, binary: Path) -> None:
    """Drop ports files a killed nats-server left behind, so none can pass for a new server's."""
    for stale in directory.glob(f"{binary.name}_*.ports"):
        stale.unlink(missing_ok=True)


def server_command(
    binary: Path, *, host: str, port: int, monitor_port: int, store: Path, ports: Path | None = None
) -> list[str]:
    command = [
        str(binary),
        "--config", str(nats_config()),
        "--addr", host,
        "--port", str(port),
        "--http_port", str(monitor_port),
        "--store_dir", str(store),
    ]
    if ports is not None:
        command += ["--ports_file_dir", str(ports)]
    return command


def start(command: list[str], log: IO[bytes]) -> subprocess.Popen:
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

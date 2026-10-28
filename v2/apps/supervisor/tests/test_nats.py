from __future__ import annotations

import hashlib
import io
import socket
import tarfile
from pathlib import Path

import pytest

from openengine.supervisor.processes import nats

TARGET = ("linux", "arm64")
STEM = f"nats-server-v{nats.NATS_VERSION}-linux-arm64"


def _release(directory: Path, binary: bytes = b"#!/bin/sh\n") -> str:
    """A release directory holding one archive laid out like the real ones; returns its sha256."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"{STEM}/nats-server")
        info.size = len(binary)
        tar.addfile(info, io.BytesIO(binary))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{STEM}.tar.gz").write_bytes(buffer.getvalue())
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def test_installs_the_verified_binary_as_an_executable(tmp_path: Path) -> None:
    release = tmp_path / "release"
    checksum = _release(release, b"server")

    binary = nats.install_nats_server(
        tmp_path / "cache", target=TARGET, release_url=release.as_uri(), checksums={TARGET: checksum}
    )

    assert binary == tmp_path / "cache" / "nats-server" / nats.NATS_VERSION / "nats-server"
    assert binary.read_bytes() == b"server"
    assert binary.stat().st_mode & 0o111


def test_refuses_an_archive_whose_checksum_does_not_match(tmp_path: Path) -> None:
    release = tmp_path / "release"
    _release(release)

    with pytest.raises(nats.ChecksumMismatch):
        nats.install_nats_server(
            tmp_path / "cache", target=TARGET, release_url=release.as_uri(), checksums={TARGET: "0" * 64}
        )

    assert not (tmp_path / "cache" / "nats-server" / nats.NATS_VERSION / "nats-server").exists()


def test_uses_the_cached_binary_without_downloading(tmp_path: Path) -> None:
    cached = tmp_path / "cache" / "nats-server" / nats.NATS_VERSION / "nats-server"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached")

    binary = nats.install_nats_server(
        tmp_path / "cache", target=TARGET, release_url=(tmp_path / "missing").as_uri(), checksums={}
    )

    assert binary == cached


def test_refuses_a_platform_without_a_pinned_build(tmp_path: Path) -> None:
    with pytest.raises(nats.UnsupportedPlatform, match="windows-amd64"):
        nats.install_nats_server(tmp_path, target=("windows", "amd64"))


def test_pins_every_supported_platform() -> None:
    assert set(nats.ARCHIVE_SHA256) == {(s, m) for s in ("darwin", "linux") for m in ("amd64", "arm64")}


def test_packages_the_infra_nats_configuration() -> None:
    infra = Path(__file__).parents[3] / "infra" / "nats" / "nats-server.conf"
    assert nats.nats_config().read_text() == infra.read_text()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_refuses_a_port_another_process_listens_on() -> None:
    with socket.create_server(("127.0.0.1", 0)) as taken:
        port = taken.getsockname()[1]

        with pytest.raises(nats.PortInUse, match=rf"127\.0\.0\.1:{port} \(the NATS client port\) is already in use"):
            nats.ensure_ports_free("127.0.0.1", port=port, monitor_port=_free_port())
        with pytest.raises(nats.PortInUse, match=rf"127\.0\.0\.1:{port} \(the NATS monitoring port\)"):
            nats.ensure_ports_free("127.0.0.1", port=_free_port(), monitor_port=port)


def test_counts_a_stopped_servers_lingering_connections_as_free() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as client:
            accepted, _ = server.accept()
            # The server closes first, so its side of the connection lingers in TIME_WAIT, as a
            # stopped nats-server's client connections do when `oe up` follows `oe down`.
            accepted.close()
            client.recv(1)

    nats.ensure_ports_free("127.0.0.1", port=port, monitor_port=port)


def test_names_the_ports_file_after_the_binary_and_pid(tmp_path: Path) -> None:
    binary = tmp_path / "bin" / "nats-server"

    assert nats.ports_file(tmp_path, binary, 42) == tmp_path / "nats-server_42.ports"
    command = nats.server_command(binary, host="127.0.0.1", port=1, monitor_port=2, store=tmp_path, ports=tmp_path)
    assert command[-2:] == ["--ports_file_dir", str(tmp_path)]
    assert "--ports_file_dir" not in nats.server_command(binary, host="127.0.0.1", port=1, monitor_port=2, store=tmp_path)


def test_removes_only_the_binarys_stale_ports_files(tmp_path: Path) -> None:
    stale = tmp_path / "nats-server_42.ports"
    stale.write_text("{}")
    other = tmp_path / "other_42.ports"
    other.write_text("{}")
    (tmp_path / "ge.ready").write_text("http://127.0.0.1:1/healthz")

    nats.remove_stale_ports_files(tmp_path, tmp_path / "nats-server")

    assert not stale.exists()
    assert other.exists()
    assert (tmp_path / "ge.ready").exists()

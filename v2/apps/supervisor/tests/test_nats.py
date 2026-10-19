from __future__ import annotations

import hashlib
import io
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

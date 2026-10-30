"""A broker identity belongs to one supervisor launch, never to a reusable port."""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import shutil
import stat
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from openengine.messaging.broker import BrokerTrust, open_private_directory, reject_extended_acl


class LoopbackRequired(ValueError):
    def __init__(self) -> None:
        super().__init__("managed NATS must bind a loopback IP address")


class UnsafeLock(PermissionError):
    def __init__(self) -> None:
        super().__init__("unsafe supervisor lock")


def _check_lock(fd: int) -> None:
    reject_extended_acl(fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise UnsafeLock


class Launch:
    """Hold the instance lock until all children have stopped and keys are retired."""

    def __init__(self, directory: Path, host: str, port: int) -> None:
        if not ipaddress.ip_address(host).is_loopback:
            raise LoopbackRequired
        self.directory = directory
        self.manifest = directory / "broker.json"
        self.launch_id = uuid.uuid4().hex
        self.runtime = directory / f"launch-{self.launch_id}"
        self.config = self.runtime / "nats.conf"
        self.host = host
        self.port = port
        self._directory_fd: int | None = None
        self._lock_fd: int | None = None
        self._published = False
        self.keep_material = False

    def __enter__(self) -> Launch:
        self._directory_fd = open_private_directory(self.directory, create=True)
        try:
            self._lock_fd = os.open(
                ".supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600, dir_fd=self._directory_fd,
            )
            _check_lock(self._lock_fd)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.runtime.mkdir(mode=0o700)
            runtime_fd = open_private_directory(self.runtime)
            os.close(runtime_fd)
            self._create_identity()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _write(self, path: Path, data: bytes) -> None:
        # Exclusive creation refuses an existing file or symlink. The enclosing
        # private directory is writable only by this trusted OS identity.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            reject_extended_acl(stream.fileno())
            stream.write(data)

    def _create_identity(self) -> None:
        now = datetime.now(timezone.utc)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        broker_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"OE launch {self.launch_id}")])
        hostname = f"broker-{self.launch_id}.localhost"
        ca = (
            x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
            .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
            .issuer_name(ca_name).public_key(broker_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode("ascii")
        # The CA signing key is never persisted or given to a child. This CA
        # signs exactly one server identity; every client pins this launch CA.
        del ca_key
        self._write(self.runtime / "broker.pem", cert.public_bytes(serialization.Encoding.PEM))
        self._write(self.runtime / "broker.key", broker_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        ))
        host = f"[{self.host}]" if ":" in self.host else self.host
        self.trust = BrokerTrust(f"tls://{host}:{self.port}", ca_pem, hostname, self.launch_id)
        config = {
            "server_name": f"openengine-{self.launch_id}", "jetstream": True,
            "tls": {"cert_file": str(self.runtime / "broker.pem"),
                    "key_file": str(self.runtime / "broker.key"), "handshake_first": True},
        }
        self._write(self.config, json.dumps(config).encode())
        record = {"version": 1, "url": self.trust.url, "ca_pem": ca_pem,
                  "server_hostname": hostname, "launch_id": self.launch_id}
        temporary = self.runtime / "manifest.json"
        self._write(temporary, json.dumps(record).encode())
        # Replace the manifest atomically; readers see one complete generation.
        os.replace(temporary, self.manifest)
        self._published = True

    def __exit__(self, *_: object) -> None:
        error: BaseException | None = None
        try:
            if self._published and not self.keep_material:
                self.manifest.unlink(missing_ok=True)
        except BaseException as failure:  # noqa: BLE001 -- re-raised after releasing ownership
            error = failure
        try:
            if self.runtime.exists() and not self.keep_material:
                shutil.rmtree(self.runtime)
        except BaseException as failure:  # noqa: BLE001 -- re-raised after releasing ownership
            if error is None:
                error = failure
        finally:
            # Cleanup errors must never strand the OS lock or directory fd.
            # Keep the first failure while attempting every descriptor release.
            for attribute in ("_lock_fd", "_directory_fd"):
                fd = getattr(self, attribute)
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError as failure:
                        if error is None:
                            error = failure
                    finally:
                        setattr(self, attribute, None)
        if error is not None:
            raise error

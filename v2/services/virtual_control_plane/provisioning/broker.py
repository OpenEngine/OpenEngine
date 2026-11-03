"""VCP's internal runtime broker: a nats-server it owns, with one scoped user per runtime.

The broker is separate from the public `OE_NATS_URL` broker. VCP connects as
`vcp`, which may publish runtime commands and consume runtime updates and its
own reply inbox. Each runtime gets exactly the permission block of the
workspace runtime contract (`packages/contracts/internal/workspace_runtime/v1`
§Authentication) and nothing else: no `_INBOX.>`, no other runtime and no
public `oe.v1.*` subject.

A runtime's password is 32 random bytes generated here. Its plaintext lives
only in this state directory (`credentials/{runtime_id}`, 0600, in a 0700
directory, written atomically) and in the broker configuration generated from
it; its ownership row (`runtime_credentials`) is written first and stores only
a digest. The configuration is generated from `active` rows alone, so a
`revoking` or `retired` credential is never granted again, even after a
restart. `vcp`'s own password is regenerated for every launch.

`broker_process.py` owns the server through a control pipe and `broker.lock`;
see its docstring. VCP accepts only the server it launched: the per-launch
random `server_name` must match both its loopback `/varz` and the INFO of the
client port, so an unmanaged listener is never adopted or signalled.

`BrokerEndpoint` is the seam where connection verification (TLS-first, #1222)
can be adopted later; until then the broker is plaintext and must stay on a
trusted local transport.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

import nats
from nats.aio.client import Client as NATS

from virtual_control_plane.persistence.credentials import CredentialRow, CredentialRows

VCP_USER = "vcp"
RESPONSES = {"max": 1, "expires": "10s"}
STARTUP_SECONDS = 10.0
RELOAD_SECONDS = 5.0
REVOCATION_CUTOFF_SECONDS = 5.0
CLOSE_SECONDS = 5.0
REQUEST_SECONDS = 1.0
CHILD = Path(__file__).with_name("broker_process.py")
# Subject tokens and NATS user names: never `.`, `*`, `>` or whitespace.
RUNTIME_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
WILDCARD_HOSTS = ("", "0.0.0.0", "::")

log = logging.getLogger(__name__)
_OPENER = build_opener(ProxyHandler({}))


class BrokerUnavailable(RuntimeError):
    """Transient: the operation keeps its ownership and is retried."""


def _unavailable(message: str) -> NoReturn:
    raise BrokerUnavailable(message) from None


class EndpointDrift(RuntimeError):
    def __init__(self, recorded: dict, configured: dict) -> None:
        super().__init__(f"runtime broker endpoint changed from {recorded} to {configured}; restore the recorded endpoint")


class CredentialRetired(RuntimeError):
    def __init__(self, runtime_id: str, status: str) -> None:
        super().__init__(f"runtime {runtime_id} credential is {status} and is never issued again")


class CredentialConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerEndpoint:
    """Where the broker listens and the URL a guest dials to reach it."""

    host: str
    port: int
    guest_url: str

    @property
    def client_url(self) -> str:
        host = "127.0.0.1" if self.host in WILDCARD_HOSTS else self.host
        return f"nats://{host}:{self.port}"

    def record(self) -> dict:
        return {"host": self.host, "port": self.port, "guest_url": self.guest_url}


@dataclass(frozen=True)
class RuntimeCredential:
    """Where the guest connects and as whom; the password is only in `path`."""

    runtime_id: str
    url: str
    user: str
    path: Path


def runtime_permissions(runtime_id: str) -> dict:
    """Exactly the contract's per-runtime permission block."""
    commands = [f"oe.internal.v1.runtime.runtimes.{runtime_id}.{action}" for action in ("execute", "cancel", "steer", "inspect")]
    return {"publish": {"allow": [f"oe.internal.v1.vcp.runtimes.{runtime_id}.updates"]},
            "subscribe": {"allow": [*commands, f"_INBOX.oe.runtime.{runtime_id}.>"]},
            "allow_responses": RESPONSES}


def vcp_permissions(instance: str) -> dict:
    return {"publish": {"allow": ["oe.internal.v1.runtime.runtimes.>"]},
            "subscribe": {"allow": ["oe.internal.v1.vcp.runtimes.*.updates", f"_INBOX.oe.vcp.{instance}.>"]},
            "allow_responses": RESPONSES}


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def write_private(path: Path, content: bytes) -> None:
    """Replace `path` atomically with an fsynced 0600 file."""
    partial = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)


async def _quiet(error: Exception) -> None:
    log.warning("runtime broker connection error: %s", type(error).__name__)


class RuntimeBroker:
    def __init__(self, directory: Path, binary: Path, endpoint: BrokerEndpoint, rows: CredentialRows, *,
                 startup_seconds: float = STARTUP_SECONDS,
                 revocation_cutoff_seconds: float = REVOCATION_CUTOFF_SECONDS) -> None:
        self.directory, self.binary, self.endpoint, self.rows = directory, binary, endpoint, rows
        self.startup_seconds, self.revocation_cutoff_seconds = startup_seconds, revocation_cutoff_seconds
        self.credentials = directory / "credentials"
        self.config = directory / "nats.conf"
        self.instance: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._log = None
        self._server_name = ""
        self._password = ""
        self._monitor_url = ""
        self._serialize = asyncio.Lock()
        self._credentials_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def credential(self, runtime_id: str) -> RuntimeCredential:
        return RuntimeCredential(runtime_id, self.endpoint.guest_url, runtime_id, self.credentials / runtime_id)

    def vcp_options(self) -> dict:
        """nats-py options for VCP: its own inbox prefix, reconnecting indefinitely once started."""
        return {"user": VCP_USER, "password": self._password, "name": "vcp-runtime-broker",
                "inbox_prefix": f"_INBOX.oe.vcp.{self.instance}", "connect_timeout": REQUEST_SECONDS,
                "allow_reconnect": True, "max_reconnect_attempts": -1, "reconnect_time_wait": 0.5,
                "error_cb": _quiet}

    async def start(self) -> NATS:
        """Launch the owned broker and connect as `vcp`, within `startup_seconds` overall."""
        if self._process is not None:
            message = "runtime broker already started"
            raise RuntimeError(message)
        self._prepare()
        try:
            async with asyncio.timeout(self.startup_seconds):
                connection = await self._launch()
        except TimeoutError:
            await self.close()
            _unavailable(f"runtime broker did not start within {self.startup_seconds}s")
        except BaseException:
            await self.close()
            raise
        for row in await self.rows.with_status("revoking"):
            try:
                await self._finish_revocation(row)
            except BrokerUnavailable:
                log.warning("runtime credential revocation still unconfirmed", extra={"vcp.runtime_id": row.runtime_id})
        return connection

    async def issue(self, runtime_id: str, generation: int) -> RuntimeCredential:
        """The runtime's credential, created once and stable across VCP and VM restarts.

        The ownership row is written before the secret. A retired or revoking
        runtime is refused. While the broker runs, the call returns only once
        the reloaded broker authenticates the credential.
        """
        if not RUNTIME_ID.fullmatch(runtime_id):
            message = "runtime ID is not a valid broker subject token"
            raise ValueError(message)
        async with self._credentials_lock:
            return await self._issue(runtime_id, generation)

    async def _issue(self, runtime_id: str, generation: int) -> RuntimeCredential:
        _private_directory(self.credentials)
        row = await self.rows.claim(runtime_id, generation, runtime_id)
        if row.status != "active":
            raise CredentialRetired(runtime_id, row.status)
        if row.generation != generation:
            message = f"runtime {runtime_id} credential belongs to generation {row.generation}"
            raise CredentialConflict(message)
        path = self.credentials / runtime_id
        if not path.exists():
            if row.secret_digest is not None:
                message = f"runtime {runtime_id} credential secret is missing; destroy and replace the runtime"
                raise CredentialConflict(message)
            write_private(path, secrets.token_urlsafe(32).encode())
        secret = path.read_text()
        if row.secret_digest is None:
            await self.rows.record_digest(runtime_id, digest(secret))
        elif row.secret_digest != digest(secret):
            message = f"runtime {runtime_id} credential secret does not match its recorded digest"
            raise CredentialConflict(message)
        if self.running and not await self._authenticates(runtime_id, secret):
            await self._reload()
            if not await self._authenticates(runtime_id, secret):
                _unavailable("runtime broker does not yet accept the issued credential")
        return self.credential(runtime_id)

    async def revoke(self, runtime_id: str) -> None:
        """Retire the credential once the running broker confirms it no longer accepts it.

        New connections must be refused and existing ones closed within the
        revocation cutoff. Until both are confirmed the row stays `revoking`
        (excluded from every configuration) and this raises
        `BrokerUnavailable`; calling again resumes. Revocation proves nothing
        about whether the runtime's compute stopped.
        """
        async with self._credentials_lock:
            await self._revoke(runtime_id)

    async def _revoke(self, runtime_id: str) -> None:
        row = await self.rows.get(runtime_id)
        if row is None:
            return
        if row.status == "active":
            await self.rows.transition(runtime_id, "active", "revoking")
            row = await self.rows.get(runtime_id)
        if row.status == "revoking":
            if not self.running:
                _unavailable("runtime broker is not running; revocation cannot be confirmed")
            await self._reload()
            await self._finish_revocation(row)
        (self.credentials / runtime_id).unlink(missing_ok=True)

    async def close(self) -> None:
        """Close the control pipe; the child terminates its server and releases the lock."""
        process, self._process = self._process, None
        try:
            if process is not None:
                if process.stdin is not None:
                    process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), CLOSE_SECONDS)
                except TimeoutError:
                    # Our own live child, never a PID read from disk.
                    process.kill()
                    await process.wait()
        finally:
            if self._log is not None:
                self._log.close()
                self._log = None

    def _prepare(self) -> None:
        _private_directory(self.directory)
        _private_directory(self.credentials)
        if not os.access(self.binary, os.X_OK):
            _unavailable(f"runtime broker binary {self.binary} is not executable")
        recorded_path = self.directory / "endpoint.json"
        if recorded_path.exists():
            recorded = json.loads(recorded_path.read_text())
            configured = {**self.endpoint.record(), "instance": recorded["instance"]}
            if recorded != configured:
                raise EndpointDrift(recorded, configured)
        else:
            recorded = {**self.endpoint.record(), "instance": secrets.token_hex(8)}
            write_private(recorded_path, json.dumps(recorded).encode())
        self.instance = recorded["instance"]

    async def _acquire(self) -> int:
        # A previous owner's child may still be reaping its server after VCP died.
        descriptor = os.open(self.directory / "broker.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    await asyncio.sleep(0.05)
                else:
                    return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    async def _launch(self) -> NATS:
        lock = await self._acquire()
        try:
            self._server_name = f"oe-runtime-broker-{secrets.token_hex(8)}"
            self._password = secrets.token_urlsafe(32)
            shutil.rmtree(self.directory / "run", ignore_errors=True)
            run = self.directory / "run" / self._server_name
            _private_directory(run)
            await self._write_config(run)
            self._log = open(self.directory / "broker.log", "ab")  # noqa: SIM115 - held for the child's life
            os.chmod(self.directory / "broker.log", 0o600)
            self._process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(CHILD), str(lock), str(self.binary), str(self.config),
                pass_fds=(lock,), stdin=asyncio.subprocess.PIPE, stdout=self._log, stderr=self._log,
                start_new_session=True)
        finally:
            # From here only the child holds the lock, and VCP the only write end of its stdin.
            os.close(lock)
        while True:
            if self._process.returncode is not None:
                _unavailable(f"runtime broker exited during startup with status {self._process.returncode}")
            if await self._identified(run):
                connection = await nats.connect(self.endpoint.client_url, **self.vcp_options())
                if self.running:
                    log.info("runtime broker started", extra={"server.address": self.endpoint.client_url})
                    return connection
                await connection.close()
            await asyncio.sleep(0.05)

    async def _identified(self, run: Path) -> bool:
        """True once both our server's monitor and the client port report this launch's name."""
        ports = next(run.glob("*.ports"), None)
        if ports is None:
            return False
        try:
            monitor = json.loads(ports.read_text())["monitoring"][0]
        except (ValueError, KeyError, IndexError):
            return False
        if urlsplit(monitor).hostname != "127.0.0.1":
            _unavailable("runtime broker monitor is not on loopback")
        self._monitor_url = monitor
        if (await self._monitor("varz")).get("server_name") != self._server_name:
            _unavailable("runtime broker monitor reports another server")
        target = urlsplit(self.endpoint.client_url)
        try:
            reader, writer = await asyncio.open_connection(target.hostname, target.port)
        except OSError:
            return False
        try:
            line = await asyncio.wait_for(reader.readline(), REQUEST_SECONDS)
        finally:
            writer.close()
        if not line.startswith(b"INFO "):
            return False
        if json.loads(line[5:]).get("server_name") != self._server_name:
            _unavailable("an unmanaged listener holds the runtime broker endpoint")
        return True

    async def _monitor(self, path: str) -> dict:
        def get() -> dict:
            with _OPENER.open(f"{self._monitor_url}/{path}", timeout=REQUEST_SECONDS) as response:
                return json.load(response)
        try:
            return await asyncio.to_thread(get)
        except (OSError, ValueError) as error:
            _unavailable(f"runtime broker monitor unavailable: {type(error).__name__}")

    async def _write_config(self, run: Path) -> None:
        users = [{"user": VCP_USER, "password": self._password, "permissions": vcp_permissions(self.instance)}]
        for row in await self.rows.with_status("active"):
            if (secret := self._secret(row)) is not None:
                users.append({"user": row.username, "password": secret, "permissions": runtime_permissions(row.runtime_id)})
        config = {"server_name": self._server_name, "listen": f"{self.endpoint.host}:{self.endpoint.port}",
                  "http": "127.0.0.1:-1", "ports_file_dir": str(run), "authorization": {"users": users}}
        write_private(self.config, json.dumps(config, indent=2).encode())

    def _secret(self, row: CredentialRow) -> str | None:
        path = self.credentials / row.runtime_id
        try:
            secret = path.read_text()
        except FileNotFoundError:
            secret = None
        if secret is None or row.secret_digest is None or digest(secret) != row.secret_digest:
            # Not yet fully issued, or tampered with: never granted.
            return None
        return secret

    async def _reload(self) -> None:
        async with self._serialize:
            if not self.running:
                _unavailable("runtime broker is not running")
            before = (await self._monitor("varz")).get("config_load_time")
            await self._write_config(self.directory / "run" / self._server_name)
            self._process.stdin.write(b"R")
            await self._process.stdin.drain()
            deadline = asyncio.get_running_loop().time() + RELOAD_SECONDS
            while (await self._monitor("varz")).get("config_load_time") == before:
                if asyncio.get_running_loop().time() > deadline:
                    _unavailable("runtime broker did not reload its configuration")
                await asyncio.sleep(0.05)

    async def _finish_revocation(self, row: CredentialRow) -> None:
        path = self.credentials / row.runtime_id
        secret = path.read_text() if path.exists() else None
        deadline = asyncio.get_running_loop().time() + self.revocation_cutoff_seconds
        while any(connection.get("authorized_user") == row.username
                  for connection in (await self._monitor("connz?auth=1&limit=100000")).get("connections") or []):
            if asyncio.get_running_loop().time() > deadline:
                _unavailable("revoked runtime connections were not closed within the cutoff")
            await asyncio.sleep(0.05)
        if secret is not None and await self._authenticates(row.username, secret):
            _unavailable("runtime broker still accepts a revoked credential")
        await self.rows.transition(row.runtime_id, "revoking", "retired")
        path.unlink(missing_ok=True)
        log.info("runtime credential retired", extra={"vcp.runtime_id": row.runtime_id})

    async def _authenticates(self, user: str, secret: str) -> bool:
        try:
            probe = await nats.connect(self.endpoint.client_url, user=user, password=secret, allow_reconnect=False,
                                       max_reconnect_attempts=0, connect_timeout=REQUEST_SECONDS, error_cb=_quiet)
        except nats.errors.Error as error:
            if "authorization violation" in str(error).lower():
                return False
            _unavailable(f"runtime broker probe failed: {type(error).__name__}")
        except (OSError, TimeoutError):
            _unavailable("runtime broker probe could not connect")
        await probe.close()
        return True

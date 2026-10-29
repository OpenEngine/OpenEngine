"""Launch-scoped broker identity and the single managed NATS connection path."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import json
import os
import ssl
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nats.aio.client import Client
from nats.aio.transport import TcpTransport

_MAX_MANIFEST_BYTES = 65536
_TLS_OPTIONS = {"tls", "tls_hostname", "tls_handshake_first", "servers"}



def _validate_url(url: str) -> None:
    if not isinstance(url, str):
        message = "broker URL must be a string"
        raise ValueError(message)  # noqa: TRY004
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"nats", "tls"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.port is None
    ):
        message = "broker URL must contain only a NATS/TLS host and explicit port"
        raise ValueError(message)


@dataclass(frozen=True)
class BrokerTrust:
    url: str
    ca_pem: str
    server_hostname: str
    launch_id: str

    def __post_init__(self) -> None:
        _validate_url(self.url)
        for name in ("ca_pem", "server_hostname", "launch_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                message = f"broker trust requires {name}"
                raise ValueError(message)


def open_private_directory(path: Path, create: bool = False) -> int:
    """Return an owned directory fd; never follow symlinks in its ancestry.

    Root-owned sticky temporary directories may contain the private directory.
    Its parent cannot replace a child owned by this UID through the sticky bit.
    Same-UID processes are deliberately inside this filesystem trust boundary.
    """
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 2:
        message = "private runtime directory must be an absolute non-root path"
        raise ValueError(message)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open(path.anchor, flags)
    try:
        for index, component in enumerate(path.parts[1:]):
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=current)
            try:
                final = index == len(path.parts) - 2
                _check_directory(os.fstat(child), final=final)
                reject_extended_acl(child, ancestor=not final)
            except BaseException:
                os.close(child)
                raise
            os.close(current)
            current = child
        result = current
        current = -1
        return result
    finally:
        if current >= 0:
            os.close(current)


def _check_directory(info: os.stat_result, *, final: bool) -> None:
    if final:
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            message = "runtime directory must be private and owned by this user"
            raise PermissionError(message)
    else:
        sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_uid not in {0, os.getuid()} or (info.st_mode & 0o022 and not sticky_root):
            message = "runtime directory has an untrusted writable ancestor"
            raise PermissionError(message)


def load_trust(path: Path) -> BrokerTrust:
    """Read one protected manifest through directory fds, not a checked pathname."""
    path = Path(path)
    parent = open_private_directory(path.parent)
    try:
        # NONBLOCK avoids hanging on a substituted FIFO before fstat rejects it.
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            reject_extended_acl(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                message = "broker trust must be a private regular file owned by this user"
                raise PermissionError(message)
            if info.st_size > _MAX_MANIFEST_BYTES:
                message = "broker trust manifest is too large"
                raise ValueError(message)
            with os.fdopen(fd, "rb", closefd=False) as source:
                raw = source.read(_MAX_MANIFEST_BYTES + 1)
            if len(raw) > _MAX_MANIFEST_BYTES:
                message = "broker trust manifest is too large"
                raise ValueError(message)
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        message = "invalid broker trust JSON"
        raise ValueError(message) from error
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "url", "ca_pem", "server_hostname", "launch_id"}
        or type(data["version"]) is not int
        or data["version"] != 1
    ):
        message = "unsupported broker trust manifest"
        raise ValueError(message)
    return BrokerTrust(**{key: value for key, value in data.items() if key != "version"})


async def connect(url: str, *, trust: BrokerTrust | None = None, **options: Any) -> Client:
    """Authenticate before INFO/CONNECT, retaining the same trust on reconnect."""
    forbidden = _TLS_OPTIONS.intersection(options)
    if forbidden:
        message = f"broker security options cannot be overridden: {', '.join(sorted(forbidden))}"
        raise ValueError(message)
    if trust is None:
        # External/test plaintext connections remain explicit; managed callers
        # must load their manifest before entering this compatibility path.
        if not isinstance(url, str) or urlsplit(url).scheme != "nats":
            message = "TLS broker connections require explicit trust"
            raise ValueError(message)
        return await _connect_owned(url, **options)
    if url != trust.url:
        message = "broker endpoint does not match this launch's trust"
        raise ValueError(message)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=trust.ca_pem)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    # Without TLS-first, nats-py can let an unauthenticated INFO decide whether
    # to upgrade. No protocol bytes or credentials may precede verification.
    return await _connect_owned(
        url,
        tls=context,
        tls_hostname=trust.server_hostname,
        tls_handshake_first=True,
        **options,
    )


async def _connect_owned(url: str, **options: Any) -> Client:
    client = Client()
    try:
        await client.connect(url, **options)
    except BaseException:
        # nats-py leaves failed initial connections open when retries are off.
        # Retain the client so authentication failures cannot leak transports.
        closing = asyncio.create_task(client.close())
        try:
            await asyncio.wait({closing}, timeout=1.0)
        finally:
            if not closing.done():
                closing.cancel()
                transport = client._transport
                if isinstance(transport, TcpTransport) and transport._io_writer is not None:
                    transport._io_writer.transport.abort()
                elif transport is not None:
                    transport.close()
            closing.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        raise
    else:
        return client


def reject_extended_acl(fd: int, *, ancestor: bool = False) -> None:
    """Mode bits do not describe ACL grants; inspect the already-open object.

    Deny-only Darwin ACLs on ancestors are harmless (macOS home directories
    commonly deny deletion). Private objects must have no extended ACL at all.
    Unsupported inspection fails closed rather than assuming mode bits suffice.
    """
    message = "extended ACL grants are not allowed on broker trust storage"
    if sys.platform == "darwin":
        _check_darwin_acl(fd, ancestor=ancestor)
    elif sys.platform.startswith("linux"):
        names = ["system.posix_acl_access"]
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            names.append("system.posix_acl_default")
        for name in names:
            try:
                os.getxattr(fd, name)
            except OSError as error:
                if error.errno != errno.ENODATA:
                    raise
            else:
                raise PermissionError(message)
    else:
        message = "broker trust ACL inspection is unsupported on this platform"
        raise PermissionError(message)


def _check_darwin_acl(fd: int, *, ancestor: bool) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    library.acl_get_fd_np.restype = ctypes.c_void_p
    library.acl_get_entry.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
    library.acl_get_entry.restype = ctypes.c_int
    library.acl_get_tag_type.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    library.acl_get_tag_type.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    acl = library.acl_get_fd_np(fd, 0x100)  # Darwin ACL_TYPE_EXTENDED.
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:  # No extended ACL exists.
            return
        raise OSError(error, os.strerror(error))
    try:
        entry = ctypes.c_void_p()
        flag = 0  # ACL_FIRST_ENTRY; subsequent calls use ACL_NEXT_ENTRY (-1).
        while library.acl_get_entry(acl, flag, ctypes.byref(entry)) == 0:
            _check_darwin_entry(library, entry, ancestor=ancestor)
            flag = -1
        error = ctypes.get_errno()
        if error != errno.EINVAL:  # Darwin reports end-of-ACL as EINVAL.
            raise OSError(error, os.strerror(error))
    finally:
        library.acl_free(acl)


def _check_darwin_entry(library: Any, entry: Any, *, ancestor: bool) -> None:
    tag = ctypes.c_int()
    if library.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if not ancestor or tag.value != 2:  # Darwin ACL_EXTENDED_DENY.
        message = "extended ACL grants are not allowed on broker trust storage"
        raise PermissionError(message)

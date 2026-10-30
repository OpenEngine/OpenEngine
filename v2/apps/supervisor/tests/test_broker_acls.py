"""Native filesystem ACLs must not widen the broker's protected OS identity.

These tests modify only private temporary paths. They verify rejection of ACL
metadata, not a successful attack by a separately logged-in OS principal.
"""

from __future__ import annotations

import errno
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from openengine.messaging.broker import load_trust
from openengine.supervisor.broker_launch import Launch

MACOS = pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACL semantics")
LINUX = pytest.mark.skipif(sys.platform != "linux", reason="Linux POSIX ACL metadata")


def _mac_acl(path: Path, entry: str) -> None:
    subprocess.run(["/bin/chmod", "+a", entry, str(path)], check=True, capture_output=True, text=True)


@MACOS
def test_inheritable_allow_acl_is_rejected_before_broker_keys_exist(tmp_path: Path) -> None:
    state = tmp_path.resolve() / "state"
    state.mkdir(mode=0o700)
    _mac_acl(state, "everyone allow read,write,execute,file_inherit,directory_inherit")
    assert state.stat().st_mode & 0o777 == 0o700

    with pytest.raises(PermissionError):
        with Launch(state, "127.0.0.1", 4222):
            pytest.fail("private mode bits must not conceal inherited ACL grants")

    assert not list(state.glob("launch-*"))
    assert not (state / "broker.json").exists()


@MACOS
def test_manifest_allow_acl_is_rejected_even_with_mode_0600(tmp_path: Path) -> None:
    with Launch(tmp_path.resolve() / "state", "127.0.0.1", 4222) as launch:
        _mac_acl(launch.manifest, "everyone allow read,write")
        assert launch.manifest.stat().st_mode & 0o777 == 0o600

        with pytest.raises(PermissionError):
            load_trust(launch.manifest)


@MACOS
def test_ancestor_allow_acl_cannot_replace_the_protected_namespace(tmp_path: Path) -> None:
    ancestor = tmp_path.resolve() / "ancestor"
    ancestor.mkdir(mode=0o700)
    state = ancestor / "state"
    state.mkdir(mode=0o700)
    _mac_acl(ancestor, "everyone allow write,delete_child,execute")
    assert ancestor.stat().st_mode & 0o777 == 0o700

    with pytest.raises(PermissionError):
        with Launch(state, "127.0.0.1", 4222):
            pytest.fail("an ancestor ACL must not permit namespace substitution")

    assert not list(state.glob("launch-*"))


@MACOS
def test_deny_only_ancestor_acl_does_not_widen_access(tmp_path: Path) -> None:
    ancestor = tmp_path.resolve() / "ancestor"
    ancestor.mkdir(mode=0o700)
    # Standard macOS home directories commonly carry this protective entry.
    _mac_acl(ancestor, "everyone deny delete")
    with Launch(ancestor / "state", "127.0.0.1", 4222) as launch:
        assert load_trust(launch.manifest) == launch.trust


def _linux_acl(path: Path, name: str) -> None:
    # ACL_USER_OBJ, named ACL_USER, ACL_GROUP_OBJ, ACL_MASK, ACL_OTHER.
    # A directory default ACL does not change its own 0700 mode bits.
    entries = [(1, 7, 0xFFFFFFFF), (2, 7, 65534), (4, 0, 0xFFFFFFFF),
               (16, 7, 0xFFFFFFFF), (32, 0, 0xFFFFFFFF)]
    value = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    try:
        os.setxattr(path, name, value)
    except OSError as error:
        if error.errno in {errno.ENOTSUP, errno.EOPNOTSUPP}:
            pytest.skip("test filesystem does not support POSIX ACLs")
        raise


@LINUX
def test_linux_default_acl_is_rejected_before_key_creation(tmp_path: Path) -> None:
    state = tmp_path.resolve() / "state"
    state.mkdir(mode=0o700)
    _linux_acl(state, "system.posix_acl_default")
    assert state.stat().st_mode & 0o777 == 0o700

    with pytest.raises(PermissionError):
        with Launch(state, "127.0.0.1", 4222):
            pytest.fail("unqualified inherited ACL policy must not enter key provisioning")

    assert not list(state.glob("launch-*"))

"""Linux cgroup-v2 supervision for real executors: the trusted side of the process boundary.

The daemon runs as root; the agent runs as a separate unprivileged UID/GID that
the operator provisions. Each launch gets its own cgroup, named before launch
intent commits (`plan`), so a restarted daemon can always find and fence it. The
trusted launcher (`launch.py`) joins that cgroup and drops privileges before it
executes the agent, with a fixed environment; a request chooses none of the
executable, environment or UID. Fencing writes `cgroup.kill` until the kernel
reports the whole tree unpopulated, then removes the cgroup, so a launcher that
had not joined yet can never join it afterwards. A timeout is not proof of
death: fencing then reports False and the attempt stays owned.

Construction fails closed: there is no fallback or test-only fence in the
installed runtime. Tests supply their own fixture supervisor.
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import sys
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

LAUNCHER = Path(__file__).with_name("launch.py")
NAME = re.compile(r"^exec_[0-9a-f]{32}$")


class Unsupervised(StrEnum):
    HOST = "real executors need a Linux daemon running as root"
    ACCOUNT = "the agent needs an unprivileged UID and GID"
    CGROUP = "the executor root must be a root-owned, delegated cgroup-v2 directory"
    KILL = "the kernel lacks cgroup.kill"
    WRITABLE = "supervision or journal state is writable by the agent account"
    CONFIGURATION = "--codex-acp needs an absolute path, a positive prompt timeout, --cgroup-root, --agent-uid, --agent-gid and --agent-home"


class SupervisionUnavailable(RuntimeError):
    """This host cannot confine and fence an executor's process tree."""

    def __init__(self, reason: Unsupervised, *evidence: object) -> None:
        super().__init__(reason, *evidence)
        self.reason = reason


class InvalidIdentity(ValueError):
    """A persisted launch identity this supervisor did not plan, or from an earlier boot."""


def _protected(path: Path, uid: int) -> None:
    """Supervision and journal state the workload must not be able to write."""
    status = path.stat()
    if status.st_uid == uid or status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SupervisionUnavailable(Unsupervised.WRITABLE, str(path))


class CgroupSupervisor:
    def __init__(self, root: Path, *, uid: int, gid: int, home: Path, protected: tuple[Path, ...] = (),
                 fence_seconds: float = 10.0) -> None:
        if os.uname().sysname != "Linux" or os.geteuid() != 0:
            raise SupervisionUnavailable(Unsupervised.HOST)
        if uid <= 0 or gid <= 0:
            raise SupervisionUnavailable(Unsupervised.ACCOUNT)
        self.root, self.home = root.resolve(strict=True), home.resolve(strict=True)
        self.uid, self.gid, self.fence_seconds = uid, gid, fence_seconds
        if not (self.root / "cgroup.controllers").is_file() or self.root.stat().st_uid != 0:
            raise SupervisionUnavailable(Unsupervised.CGROUP, str(self.root))
        for path in (self.root, LAUNCHER, *protected):
            _protected(path, uid)
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def plan(self) -> dict[str, Any]:
        return {"kind": "cgroup_v2", "root": str(self.root), "boot_id": self.boot_id, "name": "exec_" + uuid4().hex}

    def _group(self, handle: Any) -> Path:
        if (not isinstance(handle, dict) or handle.get("kind") != "cgroup_v2" or handle.get("root") != str(self.root)
                or not isinstance(handle.get("name"), str) or not NAME.match(handle["name"])):
            raise InvalidIdentity(handle)
        return self.root / handle["name"]

    def command(self, handle: dict[str, Any], argv: list[str]) -> list[str]:
        """Create this launch's cgroup and return the launcher's argv. Never adopts an existing cgroup."""
        group = self._group(handle)
        if handle["boot_id"] != self.boot_id:
            raise InvalidIdentity(handle)
        group.mkdir(mode=0o755)
        if not (group / "cgroup.kill").exists():
            group.rmdir()
            raise SupervisionUnavailable(Unsupervised.KILL)
        return [sys.executable, "-I", str(LAUNCHER), str(group), str(self.uid), str(self.gid), str(self.home), *argv]

    async def fence(self, handle: Any) -> bool:
        """True once the launch's whole process tree is empty and its cgroup removed."""
        try:
            group = self._group(handle)
        except InvalidIdentity:
            return False
        if handle.get("boot_id") != self.boot_id:
            return True  # The host rebooted since: nothing from that launch survives.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.fence_seconds
        while loop.time() < deadline:
            try:
                if not group.exists():
                    return True
                (group / "cgroup.kill").write_text("1")
                events = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
                if events.get("populated") == "0":
                    group.rmdir()
                    return True
            except FileNotFoundError:
                continue
            except OSError:
                pass  # for example EBUSY while the kernel is still reaping
            await asyncio.sleep(0.05)
        return False

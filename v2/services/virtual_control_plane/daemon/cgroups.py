"""Linux cgroup-v2 process fencing; names are allocated before launch intent commits."""
import asyncio
import os
from pathlib import Path
import sys

from virtual_control_plane.domain.lifecycle import identity
from virtual_control_plane.domain.runtime import invalid


class CgroupFence:
    def __init__(self, root, *, uid, gid, home):
        self.root = Path(root).resolve(strict=True)
        self.home = Path(home).resolve(strict=True)
        self.uid, self.gid = uid, gid
        if sys.platform != 'linux' or os.geteuid() != 0 or uid <= 0 or gid <= 0:
            invalid('Real executors require a root Linux daemon and an unprivileged agent UID/GID')
        if not (self.root / 'cgroup.controllers').exists():
            invalid('Executor root must be a delegated cgroup-v2 directory')
        if self.root.stat().st_uid != 0 or self.root.stat().st_mode & 0o022:
            invalid('Cgroup root must be root-owned and not writable by the agent')
        self.boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def plan(self):
        return {'kind': 'cgroup_v2', 'root': str(self.root), 'boot_id': self.boot_id, 'name': identity('exec')}

    def path(self, handle):
        name = handle.get('name', '')
        if (handle.get('kind') != 'cgroup_v2' or handle.get('root') != str(self.root)
                or not name.startswith('exec_') or len(name) != 37 or any(char not in '0123456789abcdef' for char in name[5:])):
            invalid('Invalid persisted executor fencing identity')
        return self.root / name

    def command(self, handle, executable):
        group = self.path(handle)
        if handle['boot_id'] != self.boot_id:
            invalid('Cannot launch an identity from an earlier boot')
        group.mkdir()  # Never adopt or relaunch an existing process group.
        if not (group / 'cgroup.kill').exists():
            group.rmdir()
            invalid('Kernel cgroup.kill support is required')
        return [sys.executable, '-I', str(Path(__file__).with_name('launch.py')), str(group),
                str(self.uid), str(self.gid), str(self.home), *executable]

    async def fence(self, handle):
        group = self.path(handle)
        if handle['boot_id'] != self.boot_id:
            return True  # Host reboot is stronger evidence than a reused PID.
        deadline = asyncio.get_running_loop().time() + 10
        while group.exists():
            try:
                (group / 'cgroup.kill').write_text('1')
                populated = dict(line.split() for line in (group / 'cgroup.events').read_text().splitlines())
                if populated.get('populated') == '0':
                    # Removal prevents a delayed, not-yet-attached launcher from
                    # entering the old identity after we confirmed it empty.
                    group.rmdir()
                    return True
            except FileNotFoundError:
                if not group.exists():
                    return True
                raise
            except OSError:
                if asyncio.get_running_loop().time() >= deadline:
                    return False
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(.05)
        return True

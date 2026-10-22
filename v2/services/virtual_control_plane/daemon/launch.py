"""Trusted launcher: join the persisted cgroup and drop privileges before agent exec.

Invoked by absolute filename with Python -I. It imports no workspace code and
never creates its cgroup, so a fenced launch intent cannot resurrect itself.
"""
import os
from pathlib import Path
import sys


def main():
    group, uid, gid, home, *command = sys.argv[1:]
    (Path(group) / 'cgroup.procs').write_text(str(os.getpid()))
    os.setgroups([])
    os.setgid(int(gid))
    os.setuid(int(uid))
    # Authentication is provisioned for the guest agent account, never carried
    # in a VCP command. Do not forward daemon or NATS credentials/environment.
    environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': home, 'LANG': 'C.UTF-8'}
    os.execve(command[0], command, environment)


if __name__ == '__main__':
    main()

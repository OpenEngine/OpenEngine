"""Trusted launcher: join the launch's cgroup, drop privileges and execute the agent.

Run by `CgroupSupervisor` as `python -I launch.py GROUP UID GID HOME ARGV...`.
It imports nothing from the workspace or the daemon and never creates its
cgroup: if the launch was fenced first, joining fails and the agent never runs.
Agent authentication is provisioned in the agent account's HOME by the
operator; daemon and broker credentials and environment are not forwarded.
"""

import ctypes
import os
import sys

PR_SET_NO_NEW_PRIVS = 38


def main() -> None:
    group, uid, gid, home, *argv = sys.argv[1:]
    with open(os.path.join(group, "cgroup.procs"), "w") as procs:
        procs.write(str(os.getpid()))
    os.setgroups([])
    os.setgid(int(gid))
    os.setuid(int(uid))
    if os.getuid() == 0 or ctypes.CDLL(None, use_errno=True).prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SystemExit(126)  # privileges were not dropped: never run the agent
    os.umask(0o077)
    os.execve(argv[0], argv, {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8"})


if __name__ == "__main__":
    main()

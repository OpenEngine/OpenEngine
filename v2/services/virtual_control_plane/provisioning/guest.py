"""Root guest supervisor. Configuration is installed by the provider, never a task."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

CONFIG = Path('/etc/oe-runtime.json')
JOURNAL = Path('/var/lib/oe-runtime')
READY = JOURNAL / 'listening'


def supervise():
    config = json.loads(CONFIG.read_text())
    Path('/sys/fs/cgroup/oe-runtime').mkdir(mode=0o755, exist_ok=True)
    JOURNAL.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [sys.executable, '-m', 'virtual_control_plane.daemon.service',
               '--workspace-id', config['workspace_id'], '--runtime-id', config['runtime_id'],
               '--generation', str(config['generation']), '--workspace-root', '/workspace',
               '--journal-directory', str(JOURNAL), '--connection-file', str(CONFIG),
               '--codex-acp', config['adapter'], '--cgroup-root', '/sys/fs/cgroup/oe-runtime',
               '--agent-uid', '1000', '--agent-gid', '1000', '--agent-home', '/home/oe-agent',
               '--ready-file', str(READY)]
    stopping = False
    child = None

    def stop(*_):
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping:
        READY.unlink(missing_ok=True)
        initialize = not (JOURNAL / 'binding.json').exists()
        # Pending first setup may resume; established journal loss fails closed.
        with (JOURNAL / 'daemon.log').open('ab') as log:
            child = subprocess.Popen([*command, *(['--initialize'] if initialize else [])], stdout=log, stderr=log)
            (JOURNAL / 'daemon.pid').write_text(str(child.pid))
            child.wait()
        if not stopping:
            time.sleep(1)


def bootstrap(bundle, config_path):
    if os.geteuid() != 0:
        message = 'Guest bootstrap requires root'
        raise PermissionError(message)
    config = json.loads(Path(config_path).read_text())
    if CONFIG.exists():
        existing = json.loads(CONFIG.read_text())
        if existing != config:
            message = 'Guest runtime binding changed'
            raise ValueError(message)
        Path(config_path).unlink()
        return
    import pwd
    import grp
    try:
        account = pwd.getpwnam('oe-agent')
        if account.pw_uid != 1000 or account.pw_gid != 1000:
            message = 'Agent account has unexpected identity'
            raise ValueError(message)
    except KeyError:
        try:
            grp.getgrgid(1000)
        except KeyError:
            subprocess.run(['groupadd', '-g', '1000', 'oe-agent'], check=True)
        subprocess.run(['useradd', '-u', '1000', '-g', '1000', '-m', '-s', '/bin/sh', 'oe-agent'], check=True)
    group = Path('/sys/fs/cgroup/oe-runtime')
    if not (group.parent / 'cgroup.controllers').exists():
        message = 'Guest requires cgroup v2'
        raise RuntimeError(message)
    group.mkdir(mode=0o755, exist_ok=True)
    group.chmod(0o755)
    for root in (Path('/workspace'), Path('/home/oe-agent')):
        root.mkdir(parents=True, exist_ok=True)
        for directory, directories, files in os.walk(root, followlinks=False):
            for path in [Path(directory), *[Path(directory) / name for name in directories + files]]:
                os.chown(path, 1000, 1000, follow_symlinks=False)
    JOURNAL.mkdir(parents=True, mode=0o700, exist_ok=True)
    JOURNAL.chmod(0o700)
    subprocess.run(['python3', '-m', 'venv', '/opt/oe'], check=True)
    subprocess.run(['/opt/oe/bin/pip', 'install', '--require-hashes', '-r', str(Path(bundle) / 'requirements.txt')], check=True)
    subprocess.run(['/opt/oe/bin/pip', 'install', '--no-deps', *map(str, Path(bundle).glob('*.whl'))], check=True)
    fixture = Path(bundle) / 'fixture.py'
    if config['adapter'] == '/opt/oe/fixture':
        if not fixture.exists():
            message = 'Fixture adapter was not included in bundle'
            raise ValueError(message)
        target = Path('/opt/oe/fixture')
        target.write_text('#!/opt/oe/bin/python\n' + fixture.read_text())
        target.chmod(0o755)
    if not os.access(config['adapter'], os.X_OK):
        message = 'Configured adapter is not installed in guest'
        raise ValueError(message)
    temporary = CONFIG.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as file:
        json.dump(config, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(CONFIG)
    directory = os.open(CONFIG.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    Path(config_path).unlink()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--bootstrap', nargs=2, metavar=('BUNDLE', 'CONFIG'))
    args = parser.parse_args()
    if args.bootstrap:
        bootstrap(*args.bootstrap)
    else:
        supervise()

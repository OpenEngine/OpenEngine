"""VCP-owned internal broker with durable, per-runtime NATS users."""
import asyncio
import json
import fcntl
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit

import nats


def private_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as file:
        json.dump(value, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class RuntimeBroker:
    def __init__(self, directory, executable, port, guest_url=None, bind_address=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / 'identities.json'
        self.config = self.directory / 'nats.json'
        self.executable, self.port = executable, port
        self.bind_address = bind_address or '127.0.0.1'
        self.url = f'nats://{self.bind_address}:{port}'
        self.guest_url = guest_url or f'nats://100.96.0.1:{port}'
        self.guest_host = urlsplit(self.guest_url).hostname
        if not self.guest_host:
            message = 'Runtime guest URL must include a host'
            raise ValueError(message)
        self.process = None
        self.control_password = secrets.token_urlsafe(32)
        self.startup_timeout = 10.0
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state['endpoint'] != self.guest_url or self.state['port'] != port or self.state['bind_address'] != self.bind_address:
                message = 'Runtime broker endpoint changed; restore the persisted endpoint'
                raise ValueError(message)
        else:
            self.state = {'control': secrets.token_urlsafe(32), 'runtimes': {}, 'endpoint': self.guest_url, 'port': port, 'bind_address': self.bind_address}
            private_json(self.path, self.state)

    def configuration(self):
        users = [{'user': 'vcp', 'password': self.control_password, 'permissions': {
            'publish': ['oe.internal.v1.runtime.runtimes.>', '_INBOX.oe.runtime.>'],
            'subscribe': ['oe.internal.v1.vcp.runtimes.*.updates', '_INBOX.oe.vcp.>'],
            'allow_responses': {'max': 1, 'expires': '10s'}}}]
        for runtime_id, item in self.state['runtimes'].items():
            users.append({'user': runtime_id, 'password': item['password'], 'permissions': {
                'publish': [f'oe.internal.v1.vcp.runtimes.{runtime_id}.updates'],
                'subscribe': [*[f'oe.internal.v1.runtime.runtimes.{runtime_id}.{action}' for action in ('execute', 'cancel', 'steer', 'inspect')],
                              f'_INBOX.oe.runtime.{runtime_id}.>'],
                'allow_responses': {'max': 1, 'expires': '10s'}}})
        return {'listen': f'{self.bind_address}:{self.port}', 'authorization': {'users': users}}

    async def start(self):
        lock = (self.directory / 'broker.lock').open('a')
        try:
            async with asyncio.timeout(self.startup_timeout):
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        await asyncio.sleep(.05)
                private_json(self.config, self.configuration())
                self.log = (self.directory / 'broker.log').open('a')
                os.chmod(self.directory / 'broker.log', 0o600)
                self.process = await asyncio.create_subprocess_exec(
                    sys.executable, str(Path(__file__).with_name('broker_process.py')),
                    str(lock.fileno()), self.executable, str(self.config),
                    pass_fds=(lock.fileno(),), stdin=asyncio.subprocess.PIPE, start_new_session=True,
                    stdout=self.log, stderr=self.log)
                # Only the supervisor now owns the lock. Its stdin has exactly
                # one writer: this VCP. Hard VCP death therefore triggers EOF.
                lock.close()
                while self.process.returncode is None:
                    try:
                        connection = await self.connect()
                    except (OSError, nats.errors.Error, TimeoutError):
                        await asyncio.sleep(.05)
                    else:
                        if self.process.returncode is None:
                            return connection
                        await connection.close()
        except TimeoutError:
            pass
        except BaseException:
            await self.close()
            raise
        finally:
            lock.close()
        await self.close()
        message = 'Internal runtime broker failed to start within its deadline'
        raise RuntimeError(message)

    async def connect(self):
        connection = nats.NATS()
        try:
            async with asyncio.timeout(1):
                await connection.connect(self.url, user='vcp', password=self.control_password,
                                         inbox_prefix='_INBOX.oe.vcp.runtime', connect_timeout=.5,
                                         allow_reconnect=True, max_reconnect_attempts=-1,
                                         reconnect_time_wait=.2)
        except BaseException:
            await connection.close()
            raise
        else:
            return connection

    async def grant(self, ownership):
        runtime_id = ownership['runtime_id']
        item = self.state['runtimes'].get(runtime_id)
        binding = {key: ownership[key] for key in ('workspace_id', 'runtime_id', 'generation')}
        if item and item['binding'] != binding:
            message = 'Runtime credential binding changed'
            raise ValueError(message)
        if item is None:
            item = {'binding': binding, 'password': secrets.token_urlsafe(32)}
            self.state['runtimes'][runtime_id] = item
            await self.reload()
        return {'url': self.guest_url, 'user': runtime_id, 'password': item['password']}

    async def reload(self):
        private_json(self.path, self.state)
        private_json(self.config, self.configuration())
        if self.process is None or self.process.returncode is not None:
            message = 'Owned runtime broker is not running'
            raise RuntimeError(message)
        self.process.stdin.write(b'R')
        async with asyncio.timeout(1):
            await self.process.stdin.drain()
        # Reload is asynchronous. Daemon reconnect retries authentication until
        # the new configuration is active; no credential is logged or in argv.

    async def revoke(self, ownership):
        self.state['runtimes'].pop(ownership['runtime_id'], None)
        await self.reload()

    async def close(self):
        if self.process:
            # EOF asks the supervisor to terminate and reap its own child before
            # releasing the lock. Never signal a PID loaded from a stale file.
            self.process.stdin.close()
            await asyncio.wait_for(self.process.wait(), 5)
        if hasattr(self, 'log'):
            self.log.close()

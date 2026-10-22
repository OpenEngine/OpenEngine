"""Install a locked guest bundle, bound credentials and supervised daemon."""
import asyncio
from pathlib import Path

import nats

from virtual_control_plane.provisioning.broker import private_json
from virtual_control_plane.transport.nats.runtime_protocol import RuntimeTransport

# The persistent machine workload waits for atomic bootstrap completion. It
# runs again on VM boot; it does not rely on a host-owned detached exec session.
WORKLOAD = "import os,time; from pathlib import Path; p=Path('/etc/oe-runtime.json'); " \
           "exec('while not p.exists(): time.sleep(.2)'); " \
           "os.execv('/opt/oe/bin/python', ['/opt/oe/bin/python','-m','virtual_control_plane.provisioning.guest'])"


class RuntimeProvisioner:
    def __init__(self, broker, connection, bundle, adapter, egress_hosts=()):
        self.broker, self.connection = broker, connection
        self.egress_hosts = tuple(egress_hosts)
        self.bundle, self.adapter = Path(bundle).resolve(strict=True), adapter
        if not adapter.startswith('/'):
            message = 'Guest adapter must be an absolute path'
            raise ValueError(message)
        self.files = [*self.bundle.glob('*.whl'), self.bundle / 'requirements.txt', self.bundle / 'guest.py']
        if self.adapter == '/opt/oe/fixture':
            self.files.append(self.bundle / 'fixture.py')
        if len(list(self.bundle.glob('*.whl'))) < 2 or not all(file.is_file() for file in self.files):
            message = 'Runtime bundle is incomplete'
            raise ValueError(message)

    async def provision(self, provider, ownership):
        credentials = await self.broker.grant(ownership)
        name = ownership['name']
        await provider.command('machine', 'exec', '--name', name, '--user', '0', '--', 'mkdir', '-p', '/opt/oe-bootstrap')
        for file in self.files:
            await provider.command('machine', 'cp', '--mode', '600', str(file), f'{name}:/opt/oe-bootstrap/{file.name}')
        configuration = {key: ownership[key] for key in ('workspace_id', 'runtime_id', 'generation')}
        configuration.update(connection=credentials, adapter=self.adapter)
        path = self.broker.directory / (ownership['runtime_id'] + '.json')
        private_json(path, configuration)
        try:
            await provider.command('machine', 'cp', '--mode', '600', str(path), f'{name}:/opt/oe-bootstrap/config.json')
        finally:
            path.unlink(missing_ok=True)
        await provider.command('machine', 'exec', '--name', name, '--user', '0', '--',
                               'python3', '/opt/oe-bootstrap/guest.py', '--bootstrap',
                               '/opt/oe-bootstrap', '/opt/oe-bootstrap/config.json')
        for _ in range(60):
            if await self.inspect(ownership):
                return
            await asyncio.sleep(.5)
        message = 'Provisioned daemon did not start listening'
        raise RuntimeError(message)

    async def inspect(self, ownership):
        target = {'workspace_id': ownership['workspace_id'], 'runtime_id': ownership['runtime_id'],
                  'runtime_generation': ownership['generation']}
        transport = RuntimeTransport(self.connection)
        request = {'request_id': 'provision_probe', 'target': target, 'attempt_ids': []}
        try:
            reply = await transport.request(f"oe.internal.v1.runtime.runtimes.{ownership['runtime_id']}.inspect",
                                            transport.protocol.encode('inspect', request), 'inspect_response')
            # Listening follows journal recovery; execution admission separately
            # waits for VCP to commit runtime.ready after workspace creation.
            return reply['status'] == 'ok' and reply['target'] == target
        except (TimeoutError, OSError, ValueError, nats.errors.Error):
            return False

    async def destroy(self, ownership):
        await self.broker.revoke(ownership)

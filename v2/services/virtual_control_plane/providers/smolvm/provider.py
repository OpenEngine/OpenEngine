"""Persistent named machines. Service shutdown never destroys owned compute."""
import asyncio
import json
import os
import signal
from pathlib import Path
from virtual_control_plane.domain.lifecycle import unsupported

CAPABILITIES = {'provider': 'smolvm', 'isolation': 'microvm', 'checkpoint': False, 'suspend': False,
                'restore': 'none', 'approvals_survive_restore': False,
                'max_resources': {'cpu_millis': 64000, 'memory_mib': 65536, 'disk_gib': 1024},
                'tools': ['git', 'terminal']}


class Smolvm:
    capabilities = CAPABILITIES

    def __init__(self, image, executable='smolvm', runtime=None):
        self.image, self.executable, self.runtime = image, executable, runtime

    def validate(self, specification):
        if specification.get('resources', {}).get('cpu_millis', 1000) % 1000:
            unsupported('fractional_cpu', 'smolvm')

    async def command(self, *arguments, stdin=None):
        environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'TMPDIR', 'XDG_DATA_HOME',
                       'XDG_CACHE_HOME', 'DYLD_LIBRARY_PATH', 'SMOLVM_DATA_DIR', 'SMOLVM_LIB_DIR', 'SMOLVM_CONFIG') if key in os.environ}
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(self.executable, *arguments, env=environment,
                    stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True))
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
            raise
        if stdin is not None:
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()
        async def read(stream):
            result = bytearray()
            while chunk := await stream.read(65536):
                result.extend(chunk)
                if len(result) > 1024 * 1024:
                    message = 'smolvm output exceeded limit'
                    raise RuntimeError(message)
            return bytes(result)
        readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
        try:
            async with asyncio.timeout(300):
                output, errors = await asyncio.gather(*readers)
                await process.wait()
            if process.returncode:
                # CLI diagnostics can contain repository credentials; do not log them.
                message = f'smolvm {arguments[1]} exited with {process.returncode}'
                raise RuntimeError(message)
            return output
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await process.wait()

    async def find(self, ownership):
        machines = json.loads(await self.command('machine', 'ls', '--json'))
        for machine in machines:
            if machine['name'] == ownership['name']:
                labels = machine.get('labels', {})
                if labels.get('oe.runtime') != ownership['runtime_id'] or labels.get('oe.workspace') != ownership['workspace_id']:
                    message = 'Runtime ownership mismatch'
                    raise RuntimeError(message)
                return machine
        return None

    async def provision(self, ownership, specification):
        machine = await self.find(ownership)
        name = ownership['name']
        if machine is None:
            arguments = ['machine', 'create', '--name', name, '--image', self.image, '--net',
                         '--label', f"oe.runtime={ownership['runtime_id']}",
                         '--label', f"oe.workspace={ownership['workspace_id']}"]
            resources = specification.get('resources', {})
            for key, flag in (('cpu_millis', '--cpus'), ('memory_mib', '--mem'), ('disk_gib', '--storage')):
                if key in resources:
                    value = resources[key]
                    if key == 'cpu_millis':
                        if value % 1000:
                            message = 'smolvm supports whole vCPU limits only'
                            raise RuntimeError(message)
                        value //= 1000
                    arguments.extend([flag, str(value)])
            if self.runtime:
                from virtual_control_plane.provisioning.runtime import WORKLOAD
                arguments.extend(['--allow-cidr', '127.0.0.1/32'])
                for host in (self.runtime.broker.guest_host, 'pypi.org', 'files.pythonhosted.org', *self.runtime.egress_hosts):
                    arguments.extend(['--allow-host', host])
                await self.command(*arguments, '--user', '0', '--', 'python3', '-c', WORKLOAD)
            else:
                await self.command(*arguments, '--', 'sleep', 'infinity')
        # start is idempotent for an already running named machine.
        await self.command('machine', 'start', '--name', name)
        script = Path(__file__).with_name('prepare.py').read_text()
        await self.command('machine', 'exec', '--name', name, '--', 'python3', '-c', script,
                           ownership['runtime_id'], stdin=json.dumps(specification).encode())
        if self.runtime:
            await self.runtime.provision(self, ownership)
        if not await self.inspect(ownership):
            message = 'Runtime did not become ready'
            raise RuntimeError(message)

    async def inspect(self, ownership):
        if await self.find(ownership) is None:
            return False
        try:
            await self.command('machine', 'exec', '--name', ownership['name'], '--', 'python3', '-c',
                               "from pathlib import Path; import sys; assert Path('/oe-vcp-ready').read_text() == sys.argv[1]", ownership['runtime_id'])
        except RuntimeError:
            return False
        return await self.runtime.inspect(ownership) if self.runtime else True

    async def destroy(self, ownership, retention):
        # Milestone one creates no artifacts/checkpoints. Their empty sets satisfy
        # either retention choice; runtime storage is always explicitly removed.
        if await self.find(ownership) is not None:
            await self.command('machine', 'delete', '--name', ownership['name'], '--force')
        if await self.find(ownership) is not None:
            message = 'Runtime still exists after deletion'
            raise RuntimeError(message)

        if self.runtime:
            await self.runtime.destroy(ownership)

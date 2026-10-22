"""Public NATS requests/events against a separate VCP process; no service patching."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import nats
import pytest

pytestmark = pytest.mark.integration
V2 = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


@pytest.fixture
def cluster(tmp_path):
    binary = os.environ.get('NATS_SERVER') or shutil.which('nats-server')
    if not binary:
        pytest.skip('Set NATS_SERVER to run real NATS integration tests')
    port = free_port()
    log = (tmp_path / 'processes.log').open('w')
    broker = subprocess.Popen([binary, '-js', '-a', '127.0.0.1', '-p', str(port), '-sd', str(tmp_path / 'nats')], stdout=log, stderr=log)
    processes = [broker]
    def start(*flags, real=False):
        ready = tmp_path / 'ready'
        ready.unlink(missing_ok=True)
        entry = ['-m', 'virtual_control_plane.service'] if real else [str(V2 / 'services/virtual_control_plane/tests/fake_service.py')]
        provider_args = ['--image', os.environ['OE_VCP_TEST_IMAGE']] if real else ['--fake-state', str(tmp_path / 'compute')]
        process = subprocess.Popen([sys.executable, *entry,
                 '--nats-url', f'nats://127.0.0.1:{port}', '--local-account', '--ready-file', str(ready),
                 *provider_args, *flags],
                 env={**os.environ, 'OE_SQLITE_DIRECTORY': str(tmp_path / 'db')}, stdout=log, stderr=log)
        processes.append(process)
        deadline = time.monotonic() + 15
        while not ready.exists():
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail((tmp_path / 'processes.log').read_text())
            time.sleep(0.05)
        return process
    try:
        yield f'nats://127.0.0.1:{port}', start
    finally:
        for process in reversed(processes):
            stop(process)
        log.close()


async def request(connection, subject, body):
    if subject.startswith('query.') and not body:
        body = {'params': {}}
    response = await connection.request('oe.v1.vcp.' + subject, json.dumps(body).encode(), timeout=5)
    return json.loads(response.data)


async def completed(connection, operation_id, tries=100):
    for _ in range(tries):
        result = await request(connection, f'query.operations.{operation_id}.get', {})
        assert result['status'] == 'ok', result
        operation = result['result']['operation']
        if operation['status'] in ('succeeded', 'failed'):
            return operation
        await asyncio.sleep(0.05)
    pytest.fail('Operation did not finish')


def command(identifier, payload):
    return {'command_id': identifier, 'causal_path': [identifier], 'payload': payload}


def test_create_queries_duplicates_destroy_and_events(cluster):
    url, start = cluster
    process = start()
    async def scenario():
        connection = await nats.connect(url)
        try:
            events = await connection.jetstream().subscribe('oe.v1.vcp.events.>')
            body = command('creation', {'provider': 'fake'})
            response = await request(connection, 'control.workspaces.create', body)
            assert response['status'] == 'accepted'
            assert await request(connection, 'control.workspaces.create', body) == response
            operation = await completed(connection, response['operation_id'])
            assert operation['status'] == 'succeeded'
            workspace_id = response['resource_id']
            workspace = await request(connection, f'query.workspaces.{workspace_id}.get', {})
            assert workspace['result']['workspace']['state'] == 'ready'
            assert (await request(connection, 'query.workspaces.list', {}))['result']['items'] == [workspace['result']['workspace']]
            received = [json.loads((await events.next_msg(timeout=5)).data) for _ in range(2)]
            assert [event['event_type'] for event in received] == ['workspace.provisioning', 'workspace.ready']
            assert all(event['payload']['operation']['operation_id'] == response['operation_id'] for event in received)
            stop(process)
            start()
            assert await request(connection, 'control.workspaces.create', body) == response
            ensure = await request(connection, f'control.workspaces.{workspace_id}.ensure', command('ensure', {}))
            assert (await completed(connection, ensure['operation_id']))['result']['restored'] is False
            destroyed = await request(connection, f'control.workspaces.{workspace_id}.destroy', command('destroy', {'retention': {'artifacts': 'retain', 'checkpoints': 'delete'}}))
            assert (await completed(connection, destroyed['operation_id']))['status'] == 'succeeded'
            assert (await request(connection, f'query.workspaces.{workspace_id}.get', {}))['result']['workspace']['state'] == 'destroyed'
        finally:
            await connection.close()
    asyncio.run(scenario())


def test_provider_failure_and_invalid_payload(cluster):
    url, start = cluster
    start('--fail')
    async def scenario():
        connection = await nats.connect(url)
        try:
            bad = command('bad', {'provider': 'fake', 'unknown': True})
            rejected = await request(connection, 'control.workspaces.create', bad)
            assert rejected['error']['code'] == 'INVALID_ARGUMENT'
            assert await request(connection, 'control.workspaces.create', bad) == rejected
            response = await request(connection, 'control.workspaces.create', command('failure', {'provider': 'fake'}))
            assert (await completed(connection, response['operation_id']))['error']['code'] == 'PROVIDER_ERROR'
            workspace = await request(connection, f"query.workspaces.{response['resource_id']}.get", {})
            assert workspace['result']['workspace']['state'] == 'failed'
        finally:
            await connection.close()
    asyncio.run(scenario())


def test_crash_after_compute_creation_recovers_accepted_operation(cluster):
    url, start = cluster
    process = start('--crash')
    async def scenario():
        connection = await nats.connect(url)
        try:
            body = command('recover', {'provider': 'fake'})
            response = await request(connection, 'control.workspaces.create', body)
            for _ in range(100):
                if process.poll() is not None:
                    break
                await asyncio.sleep(0.05)
            assert process.poll() == 73
            start()
            assert (await completed(connection, response['operation_id']))['status'] == 'succeeded'
            assert await request(connection, 'control.workspaces.create', body) == response
        finally:
            await connection.close()
    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get('OE_VCP_TEST_IMAGE'), reason='Set OE_VCP_TEST_IMAGE for real VM smoke test')
def test_real_smolvm_create_restart_ensure_destroy(cluster):
    url, start = cluster
    process = start(real=True)
    async def scenario():
        connection = await nats.connect(url)
        workspace_id = None
        try:
            response = await request(connection, 'control.workspaces.create', command('real_create', {
                'provider': 'smolvm', 'tools': ['terminal'],
                'resources': {'cpu_millis': 1000, 'memory_mib': 512},
                'context': {'files': [{'path': 'hello.txt', 'content': 'retained workspace'}]}}))
            assert response['status'] == 'accepted', response
            workspace_id = response['resource_id']
            operation = await completed(connection, response['operation_id'], tries=12000)
            assert operation['status'] == 'succeeded', operation
            stop(process)
            start(real=True)
            ensure = await request(connection, f'control.workspaces.{workspace_id}.ensure', command('real_ensure', {}))
            assert (await completed(connection, ensure['operation_id'], tries=1200))['status'] == 'succeeded'
        finally:
            if workspace_id:
                destroy = await request(connection, f'control.workspaces.{workspace_id}.destroy', command('real_destroy', {
                    'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}}))
                assert destroy['status'] == 'accepted', destroy
                assert (await completed(connection, destroy['operation_id'], tries=1200))['status'] == 'succeeded'
            await connection.close()
    asyncio.run(scenario())


def test_execute_acceptance_survives_process_restart(cluster):
    url, start = cluster
    process = start()

    async def scenario():
        connection = await nats.connect(url)
        try:
            created = await request(connection, 'control.workspaces.create', command('create_execution', {'provider': 'fake'}))
            assert (await completed(connection, created['operation_id']))['status'] == 'succeeded'
            workspace_id = created['resource_id']
            subject = f'control.workspaces.{workspace_id}.execute'
            body = command('execute', {'agent': {'runner': 'fake'}, 'task': {'instruction': 'Run tests'}, 'session': {'mode': 'new'}})
            accepted = await request(connection, subject, body)
            assert accepted['status'] == 'accepted', accepted
            stop(process)
            start()
            assert await request(connection, subject, body) == accepted
            operation = (await request(connection, f"query.operations.{accepted['operation_id']}.get", {}))['result']['operation']
            assert operation['status'] == 'pending'
            session_id = operation['resources']['session_id']
            session = (await request(connection, f'query.sessions.{session_id}.get', {}))['result']['session']
            assert session['state'] == 'active'
            assert len(session['attempts']) == 1
            assert session['attempts'][0]['attempt_id'] == accepted['resource_id']
            assert session['attempts'][0]['state'] == 'starting'
            sessions = await request(connection, f'query.workspaces.{workspace_id}.sessions.list', {})
            assert sessions['result']['items'] == [session]
            body['command_id'] = 'concurrent'
            body['causal_path'] = ['concurrent']
            body['payload']['session'] = {'mode': 'continue', 'session_id': session_id}
            assert (await request(connection, subject, body))['error']['code'] == 'INVALID_STATE'
        finally:
            await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['completed', 'failed', 'crash', 'codex', 'codex_cancel', 'steer'])
def test_workspace_daemon_executes_over_nats_and_recovers(cluster, tmp_path, outcome):
    url, start = cluster
    process = start('--codex-executor' if outcome in ('codex', 'codex_cancel') else '--fake-executor')
    daemons = []
    log_path = tmp_path / 'daemon.log'
    log = log_path.open('w')

    def start_daemon(workspace_id, runtime, initialize=False):
        ready = tmp_path / 'daemon-ready'
        ready.unlink(missing_ok=True)
        entry = [str(V2 / 'services/virtual_control_plane/tests/acp_service.py'), '--acp-log', str(tmp_path / 'acp.log')] if outcome in ('codex', 'codex_cancel') else ['-m', 'virtual_control_plane.daemon.service']
        if outcome == 'codex_cancel':
            entry.extend(['--acp-mode', 'cancel'])
        args = [sys.executable, *entry, '--nats-url', url,
                '--local-account', '--workspace-id', workspace_id, '--runtime-id', runtime['runtime_id'],
                '--generation', str(runtime['generation']), '--workspace-root', str(tmp_path),
                '--journal-directory', str(tmp_path / 'journal'), '--ready-file', str(ready)]
        if initialize:
            args.append('--initialize')
        daemon = subprocess.Popen(args, stdout=log, stderr=log)
        daemons.append(daemon)
        deadline = time.monotonic() + 15
        while not ready.exists():
            if daemon.poll() is not None or time.monotonic() > deadline:
                pytest.fail(log_path.read_text())
            time.sleep(.05)
        return daemon

    async def scenario():
        connection = await nats.connect(url)
        try:
            created = await request(connection, 'control.workspaces.create', command('daemon_create', {'provider': 'fake'}))
            assert (await completed(connection, created['operation_id']))['status'] == 'succeeded'
            workspace_id = created['resource_id']
            workspace = (await request(connection, f'query.workspaces.{workspace_id}.get', {}))['result']['workspace']
            runtime = workspace['runtime']
            events = await connection.jetstream().subscribe(f'oe.v1.vcp.events.workspaces.{workspace_id}.>', ordered_consumer=True)
            commands_seen = await connection.subscribe(f"oe.internal.v1.runtime.runtimes.{runtime['runtime_id']}.execute")
            daemon = start_daemon(workspace_id, runtime, initialize=True)
            agent = {'runner': 'codex'} if outcome in ('codex', 'codex_cancel') else {'runner': 'fake', 'settings': {
                'outcome': 'failed' if outcome == 'failed' else 'completed',
                # Allow the bounded VCP restart to finish before a steering
                # target expires; a two-second task races process startup.
                'delay_seconds': 30 if outcome in ('crash', 'steer') else .5}}
            body = command('daemon_execute', {'agent': agent,
                'task': {'instruction': 'Simulate work'}, 'session': {'mode': 'new'}})
            for _ in range(100):
                accepted = await request(connection, f'control.workspaces.{workspace_id}.execute', body)
                if accepted['status'] == 'accepted':
                    break
                assert accepted['error']['code'] == 'UNAVAILABLE', accepted
                await asyncio.sleep(.05)
            assert accepted['status'] == 'accepted'
            original = await commands_seen.next_msg(timeout=10)
            operation = await completed(connection, accepted['operation_id'])
            assert operation['status'] == 'succeeded'
            if outcome == 'steer':
                steer_body = command('daemon_steer', {'session_id': operation['resources']['session_id'],
                                                     'message': 'Focus on correctness'})
                steered = await request(connection, f'control.workspaces.{workspace_id}.steer', steer_body)
                assert steered['status'] == 'accepted', steered
            if outcome == 'crash':
                daemon.kill()
                daemon.wait(timeout=10)
                daemon = start_daemon(workspace_id, runtime)
            else:
                stop(process)
                restarted = start('--codex-executor' if outcome in ('codex', 'codex_cancel') else '--fake-executor')
            if outcome == 'steer':
                assert (await completed(connection, steered['operation_id']))['status'] == 'succeeded'
                assert await request(connection, f'control.workspaces.{workspace_id}.steer', steer_body) == steered
                history = await request(connection, f"query.sessions.{operation['resources']['session_id']}.events.list",
                                        {'params': {'kind': ['steering']}})
                assert [row['status'] for row in history['result']['items']] == ['applied']
            if outcome == 'codex_cancel':
                unsupported = await request(connection, f'control.workspaces.{workspace_id}.steer',
                                            command('unsupported_steer', {'session_id': operation['resources']['session_id'], 'message': 'Change focus'}))
                assert unsupported['error']['code'] == 'UNSUPPORTED_CAPABILITY'
                cancel_body = command('daemon_cancel', {'attempt_id': accepted['resource_id'], 'reason': 'Stop test task'})
                cancelled = await request(connection, f'control.workspaces.{workspace_id}.cancel', cancel_body)
                assert cancelled['status'] == 'accepted', cancelled
                stop(restarted)
                start('--codex-executor')
                result = await completed(connection, cancelled['operation_id'])
                assert result['result']['final_state'] == 'cancelled'
                assert await request(connection, f'control.workspaces.{workspace_id}.cancel', cancel_body) == cancelled
            session_id = operation['resources']['session_id']
            for _ in range(1200 if outcome == 'steer' else 200):
                session = (await request(connection, f'query.sessions.{session_id}.get', {}))['result']['session']
                if session['state'] == 'idle':
                    break
                await asyncio.sleep(.05)
            assert session['state'] == 'idle', session
            assert session['attempts'][0]['state'] == ('failed' if outcome == 'crash' else 'cancelled' if outcome == 'codex_cancel' else 'completed' if outcome in ('codex', 'steer') else outcome)
            if outcome == 'steer':
                assert 'Focus on correctness' in session['attempts'][0]['result']['summary']
            stop(daemon)
            start_daemon(workspace_id, runtime)
            # Retry the exact internal execute after both processes have restarted.
            reply = json.loads((await connection.request(original.subject, original.data, timeout=5)).data)
            assert reply['status'] == 'accepted'
            assert await request(connection, f'control.workspaces.{workspace_id}.execute', body) == accepted
            inspect = {'request_id': 'inspect_recovery', 'target': {'workspace_id': workspace_id,
                       'runtime_id': runtime['runtime_id'], 'runtime_generation': runtime['generation']},
                       'attempt_ids': [accepted['resource_id']]}
            snapshot = json.loads((await connection.request(f"oe.internal.v1.runtime.runtimes.{runtime['runtime_id']}.inspect",
                                   json.dumps(inspect).encode(), timeout=5)).data)
            assert snapshot['result']['attempts'][0]['state'] == session['attempts'][0]['state']
            received = []
            while True:
                try:
                    received.append(json.loads((await events.next_msg(timeout=1)).data))
                except nats.errors.TimeoutError:
                    break
            if outcome == 'codex':
                assert sum(event['event_type'] == 'usage.reported' for event in received) == 1
                assert sum(event['event_type'] == 'conversation.message' for event in received) == 2
            if outcome == 'steer':
                assert sum(event['event_type'] == 'steering.applied' for event in received) == 1
            task_events = [event for event in received if event['event_type'].startswith('task.')]
            assert [event['event_type'] for event in task_events] == ['task.started', 'task.' + session['attempts'][0]['state']]
            assert len({event['event_id'] for event in received}) == len(received)
            destroy = await request(connection, f'control.workspaces.{workspace_id}.destroy', command('daemon_destroy', {
                'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}}))
            assert (await completed(connection, destroy['operation_id']))['status'] == 'succeeded'
        finally:
            await connection.close()
    try:
        asyncio.run(scenario())
    finally:
        for daemon in daemons:
            stop(daemon)
        log.close()


@pytest.mark.skipif(not os.environ.get('OE_VCP_RUNTIME_BUNDLE'), reason='Set OE_VCP_RUNTIME_BUNDLE for automatic real-VM provisioning')
def test_automatically_provisioned_vm_vertical_flow(cluster, tmp_path):
    from virtual_control_plane.providers.smolvm.provider import Smolvm

    url, start = cluster
    flags = ['--runtime-bundle', os.environ['OE_VCP_RUNTIME_BUNDLE'], '--runtime-adapter', '/opt/oe/fixture',
             '--runtime-broker-binary', os.environ['NATS_SERVER'], '--runtime-port', str(free_port())]
    process = start(*flags, real=True)
    provider = Smolvm(os.environ['OE_VCP_TEST_IMAGE'])

    async def scenario():
        connection = await nats.connect(url)
        workspace_id = ownership = None
        current_process = process
        try:
            created = await request(connection, 'control.workspaces.create', command('vm_create', {'provider': 'smolvm'}))
            workspace_id = created['resource_id']
            # Compute may exist even after provisioning failure: always clean it.
            operation = await completed(connection, created['operation_id'], tries=12000)
            workspace = (await request(connection, f'query.workspaces.{workspace_id}.get', {}))['result']['workspace']
            if 'runtime' in workspace:
                ownership = {'workspace_id': workspace_id, 'runtime_id': workspace['runtime']['runtime_id'],
                             'generation': workspace['runtime']['generation'], 'name': 'oe-vcp-' + workspace['runtime']['runtime_id']}
            assert operation['status'] == 'succeeded', operation
            events = await connection.jetstream().subscribe(f'oe.v1.vcp.events.workspaces.{workspace_id}.>', ordered_consumer=True)

            async def execute_task(identifier, instruction, session_id=None):
                payload = {'agent': {'runner': 'codex'}, 'task': {'instruction': instruction},
                           'session': {'mode': 'continue', 'session_id': session_id} if session_id else {'mode': 'new'}}
                for _ in range(200):
                    accepted = await request(connection, f'control.workspaces.{workspace_id}.execute', command(identifier, payload))
                    if accepted['status'] == 'accepted':
                        break
                    assert accepted['error']['retryable'], accepted
                    await asyncio.sleep(.1)
                assert accepted['status'] == 'accepted', accepted
                started = await completed(connection, accepted['operation_id'], tries=600)
                assert started['status'] == 'succeeded', started
                return accepted, started['resources']['session_id']

            async def terminal(session_id, attempt_id):
                for _ in range(600):
                    session = (await request(connection, f'query.sessions.{session_id}.get', {}))['result']['session']
                    attempt = next(item for item in session['attempts'] if item['attempt_id'] == attempt_id)
                    if attempt['state'] in ('completed', 'cancelled', 'failed'):
                        return attempt
                    await asyncio.sleep(.1)
                pytest.fail('Attempt did not terminate')

            first, session_id = await execute_task('vm_execute', 'Report VM_TASK_OK')
            assert (await terminal(session_id, first['resource_id']))['state'] == 'completed'
            continued, _ = await execute_task('vm_continue', 'Report VM_TASK_OK again', session_id)
            assert (await terminal(session_id, continued['resource_id']))['state'] == 'completed'
            active, _ = await execute_task('vm_wait', 'WAIT_FOR_CANCEL', session_id)
            # Wait until the fixture has a live child so fencing is exercised.
            for _ in range(100):
                history = await request(connection, f'query.sessions.{session_id}.events.list',
                                        {'params': {'attempt_id': active['resource_id'], 'kind': ['message']}})
                if any(row.get('text') == 'WAITING' for row in history['result']['items']):
                    break
                await asyncio.sleep(.1)
            else:
                pytest.fail('Fixture did not launch its child')
            current_process.kill()
            current_process.wait(timeout=5)
            current_process = start(*flags, real=True)
            cancelled = await request(connection, f'control.workspaces.{workspace_id}.cancel',
                                      command('vm_cancel', {'attempt_id': active['resource_id']}))
            assert (await completed(connection, cancelled['operation_id'], tries=600))['result']['final_state'] == 'cancelled'
            assert (await terminal(session_id, active['resource_id']))['state'] == 'cancelled'
            assert await request(connection, f'control.workspaces.{workspace_id}.cancel',
                                 command('vm_cancel', {'attempt_id': active['resource_id']})) == cancelled
            await provider.command('machine', 'exec', '--name', ownership['name'], '--', 'python3', '-c',
                                   "from pathlib import Path; assert not list(Path('/sys/fs/cgroup/oe-runtime').glob('exec_*'))")
            # Supervisor restarts the daemon; persisted session resumes afterward.
            await provider.command('machine', 'exec', '--name', ownership['name'], '--', 'python3', '-c',
                                   "import os,signal; from pathlib import Path; os.kill(int(Path('/var/lib/oe-runtime/daemon.pid').read_text()), signal.SIGTERM)")
            await asyncio.sleep(2)
            resumed, _ = await execute_task('vm_after_restart', 'Report VM_TASK_OK after restart', session_id)
            assert (await terminal(session_id, resumed['resource_id']))['state'] == 'completed'
            await provider.command('machine', 'stop', '--name', ownership['name'])
            await provider.command('machine', 'start', '--name', ownership['name'])
            rebooted, _ = await execute_task('vm_after_boot', 'Report VM_TASK_OK after VM boot', session_id)
            assert (await terminal(session_id, rebooted['resource_id']))['state'] == 'completed'
            snapshot = json.loads((await provider.command('machine', 'exec', '--name', ownership['name'], '--',
                '/opt/oe/bin/python', '-c',
                "import sqlite3,json; from pathlib import Path; c=sqlite3.connect(next(Path('/var/lib/oe-runtime').glob('*.sqlite3'))); print(json.dumps([json.loads(r[0])['launch_count'] for r in c.execute('select body from daemon_attempts')]))")).decode())
            assert snapshot == [1, 1, 1, 1, 1]
            received = []
            while True:
                try:
                    received.append(json.loads((await events.next_msg(timeout=1)).data))
                except nats.errors.TimeoutError:
                    break
            assert sum(event['event_type'] == 'task.cancelled' for event in received) == 1
            assert sum(event['event_type'] == 'task.completed' for event in received) == 4
            destroyed = await request(connection, f'control.workspaces.{workspace_id}.destroy',
                command('vm_destroy', {'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}}))
            assert destroyed['status'] == 'accepted', destroyed
            assert (await completed(connection, destroyed['operation_id'], tries=1200))['status'] == 'succeeded'
            assert await provider.find(ownership) is None
        finally:
            try:
                # Assertion failures must not leave test executors/VMs running.
                # Match both persisted labels; never remove unrelated machines.
                if workspace_id:
                    for machine in json.loads(await provider.command('machine', 'ls', '--json')):
                        labels = machine.get('labels', {})
                        if labels.get('oe.workspace') == workspace_id and labels.get('oe.runtime'):
                            owned = {'workspace_id': workspace_id, 'runtime_id': labels['oe.runtime'], 'name': machine['name']}
                            await provider.destroy(owned, {'artifacts': 'delete', 'checkpoints': 'delete'})
            finally:
                await connection.close()
    asyncio.run(scenario())

import asyncio
import json
from pathlib import Path
import sys

import pytest
from virtual_control_plane.application.runtime_context import RuntimeContext
from virtual_control_plane.daemon.codex import CodexBackend, instruction_text
from virtual_control_plane.daemon.executor import ExecutionWorker
from virtual_control_plane.daemon.journal import Journal
from virtual_control_plane.daemon.outbox import UpdateOutbox
from virtual_control_plane.domain.lifecycle import identity
from virtual_control_plane.tests.test_execution import commands, execute, execute_request, query
from virtual_control_plane.tests.test_runtime_flow import LocalTransport, setup
from virtual_control_plane.transport.nats.validation import Contracts
from virtual_control_plane.workers.runtime_commands import RuntimeDispatcher


class FixtureFence:
    """Test-only fence for the fixture's single, childless process. Never deployed."""
    def __init__(self):
        self.processes = []
        self.confirmed = True
        self.fenced = []

    def plan(self):
        return {'test_identity': identity('exec')}

    def command(self, handle, executable):
        return list(executable)

    async def spawn(self, *args, **kwargs):
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        self.processes.append(process)
        return process

    async def fence(self, handle):
        self.fenced.append(handle)
        if not self.confirmed:
            return False
        for process in self.processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
        return True


async def prepare(tmp_path, mode='normal'):
    store, guest, workspace_id, old_journal, updates, _ = await setup(tmp_path)
    fence = FixtureFence()
    backend = CodexBackend([sys.executable, str(Path(__file__).with_name('acp_agent.py')), str(tmp_path / 'acp.log'), mode], fence)
    backend.spawn = fence.spawn
    journal = Journal(guest, old_journal.target, tmp_path, backends={'codex': backend})
    await journal.recover()
    sender = UpdateOutbox(journal, LocalTransport(updates.accept, 'update'))
    while await sender.run_once():
        pass
    return store, guest, workspace_id, journal, sender, fence


def test_codex_subprocess_context_progress_usage_and_continuation(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, sender, fence = await prepare(tmp_path)
        worker = ExecutionWorker(journal)
        try:
            (tmp_path / 'AGENTS.md').write_text('Report what you find. Do not edit files.')
            async with store.transaction() as tx:
                record = await tx.get('workspace_records', workspace_id)
                record['specification']['context'] = {'instructions': 'Report what you find. Do not edit files.'}
                await tx.put('workspace_records', workspace_id, record)
            body = execute_request()
            body['payload']['agent'] = {'runner': 'codex', 'model': 'test-model'}
            instance = commands(store, RuntimeContext())
            accepted = await execute(instance, workspace_id, body)
            dispatcher = RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute'))
            assert await dispatcher.run_once()
            assert await worker.run_once()
            await asyncio.gather(*worker.tasks)
            assert not await worker.run_once()
            while await sender.run_once():
                pass
            operation = (await query(store, 'operation', accepted['operation_id']))['result']['operation']
            session_id = operation['resources']['session_id']
            session = (await query(store, 'session', session_id))['result']['session']
            assert session['state'] == 'idle'
            assert session['attempts'][0]['state'] == 'completed'
            assert session['attempts'][0]['usage'] == {'input_tokens': 10, 'output_tokens': 7}
            assert fence.processes[0].returncode is not None
            async with guest.transaction() as tx:
                original_session = (await tx.get('daemon_sessions', session_id))['executor_session_id']
                handle = (await tx.get('daemon_attempts', accepted['resource_id']))['executor_handle']
                assert handle in fence.fenced
            # Recreate daemon objects, preserving the existing ACP mapping.
            journal = Journal(guest, journal.target, tmp_path, backends=journal.backends)
            await journal.recover()
            sender = UpdateOutbox(journal, sender.transport)
            while await sender.run_once():
                pass
            worker = ExecutionWorker(journal)
            continued = execute_request('continue_codex', session_id)
            continued['payload']['agent'] = body['payload']['agent']
            await execute(instance, workspace_id, continued)
            assert await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            assert await worker.run_once()
            await asyncio.gather(*worker.tasks)
            assert not await worker.run_once()
            while await sender.run_once():
                pass
            log = [json.loads(line) for line in (tmp_path / 'acp.log').read_text().splitlines()]
            assert sum(row.get('method') == 'session/new' for row in log) == 1
            loaded = next(row for row in log if row.get('method') == 'session/load')
            assert loaded['params']['sessionId'] == original_session
            prompt = next(row for row in log if row.get('method') == 'session/prompt')['params']['prompt'][0]['text']
            assert 'Do not edit files' in prompt and 'Run the tests' in prompt
            async with store.transaction() as tx:
                events = [row['event'] for row in await tx.all('outbox') if 'attempt_id' in row['event']['resources']]
                assert sum(row['event_type'] == 'conversation.message' for row in events) == 4
                assert not any('Replayed' in row['payload'].get('text', '') for row in events)
                assert sum(row['event_type'] == 'usage.reported' for row in events) == 2
            for event in events:
                Contracts().validate('events', event['event_type'].replace('.', '_') + '_event', event)
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_acp_crash_requires_confirmed_fencing_before_failure(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, sender, fence = await prepare(tmp_path, 'exit')
        worker = ExecutionWorker(journal)
        try:
            body = execute_request()
            body['payload']['agent'] = {'runner': 'codex'}
            accepted = await execute(commands(store, RuntimeContext()), workspace_id, body)
            assert await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            fence.confirmed = False
            assert await worker.run_once()
            result = await asyncio.gather(*worker.tasks, return_exceptions=True)
            assert isinstance(result[0], ValueError)
            with pytest.raises(ValueError, match='termination'):
                await worker.run_once()
            async with guest.transaction() as tx:
                assert (await tx.get('daemon_attempts', accepted['resource_id']))['state'] == 'running'
            fence.confirmed = True
            assert await worker.run_once()
            assert not await worker.run_once()
            while await sender.run_once():
                pass
            async with guest.transaction() as tx:
                attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                assert attempt['launch_count'] == 1 and attempt['state'] == 'failed'
        finally:
            fence.confirmed = True
            await fence.fence({})
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_recovery_without_executor_fence_keeps_attempt_owned(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, sender, fence = await prepare(tmp_path)
        try:
            body = execute_request()
            body['payload']['agent'] = {'runner': 'codex'}
            accepted = await execute(commands(store, RuntimeContext()), workspace_id, body)
            assert await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            async with guest.transaction() as tx:
                attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                attempt.update(state='launching', executor_handle=fence.plan(), launch_count=1)
                await tx.put('daemon_attempts', accepted['resource_id'], attempt)
            missing = Journal(guest, journal.target, tmp_path)
            with pytest.raises(ValueError, match='Cannot fence'):
                await missing.recover()
            async with guest.transaction() as tx:
                assert not (await tx.get('daemon_meta', 'runtime'))['ready']
                assert (await tx.get('daemon_attempts', accepted['resource_id']))['state'] == 'launching'
            await journal.recover()
            async with guest.transaction() as tx:
                attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                assert attempt['state'] == 'failed' and attempt['launch_count'] == 1
        finally:
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_context_rejects_symlink_instruction(tmp_path):
    (tmp_path / 'outside').write_text('not an instruction')
    (tmp_path / 'AGENTS.md').symlink_to(tmp_path / 'outside')
    with pytest.raises(OSError):
        instruction_text(tmp_path, 'AGENTS.md')


def test_usage_retry_does_not_double_count_and_overlap_is_rejected(tmp_path):
    from copy import deepcopy
    from virtual_control_plane.application.runtime_updates import RuntimeUpdates
    from virtual_control_plane.domain.lifecycle import now

    async def scenario():
        store, guest, workspace_id, journal, sender, fence = await prepare(tmp_path)
        try:
            body = execute_request()
            body['payload']['agent'] = {'runner': 'codex'}
            accepted = await execute(commands(store, RuntimeContext()), workspace_id, body)
            async with store.transaction() as tx:
                command = (await tx.all('runtime_outbox'))[0]['message']
            async with guest.transaction() as tx:
                await journal.append(tx, 'attempt.started', {}, command)
                stamp = now()
                await journal.append(tx, 'attempt.progress', {'kind': 'usage', 'period_start': '2020-01-01T00:00:00Z',
                                     'period_end': stamp, 'usage': {'input_tokens': 10, 'output_tokens': 7}}, command)
            while await sender.run_once():
                pass
            async with guest.transaction() as tx:
                message = next(row['message'] for row in await tx.all('daemon_updates') if row['message']['kind'] == 'attempt.progress')
            updates = RuntimeUpdates(store)
            subject = f"oe.internal.v1.vcp.runtimes.{journal.target['runtime_id']}.updates"
            assert (await updates.accept(subject, message))['status'] == 'committed'
            changed = deepcopy(message)
            changed.update(event_id=identity('rte'), sequence=message['sequence'] + 1)
            assert (await updates.accept(subject, changed))['error']['code'] == 'EVENT_CONFLICT'
            operation = (await query(store, 'operation', accepted['operation_id']))['result']['operation']
            session = (await query(store, 'session', operation['resources']['session_id']))['result']['session']
            assert session['attempts'][0]['usage'] == {'input_tokens': 10, 'output_tokens': 7}
        finally:
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_cgroup_identity_validation_and_previous_boot(tmp_path):
    from virtual_control_plane.daemon.cgroups import CgroupFence
    fence = CgroupFence.__new__(CgroupFence)
    fence.root, fence.boot_id = tmp_path, 'boot_current'
    handle = fence.plan()
    assert fence.path(handle).parent == tmp_path
    wrong = {**handle, 'name': '../other'}
    with pytest.raises(ValueError, match='identity'):
        fence.path(wrong)
    old = {**handle, 'boot_id': 'boot_previous'}
    assert asyncio.run(fence.fence(old))


def test_stalled_acp_stdin_times_out_fences_and_releases_attempt(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, sender, fence = await prepare(tmp_path, 'stop_reading')
        worker = ExecutionWorker(journal)
        journal.backends['codex'].prompt_timeout = .1
        try:
            body = execute_request()
            body['payload']['agent'] = {'runner': 'codex'}
            # Larger than pipe capacity, within both public and ACP wire limits.
            body['payload']['task']['instruction'] = '界' * 65536
            accepted = await execute(commands(store, RuntimeContext()), workspace_id, body)
            assert await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            assert await worker.run_once()
            done, pending = await asyncio.wait(worker.tasks, timeout=5)
            assert not pending, 'Blocked prompt write prevented timeout and fencing'
            results = await asyncio.gather(*done, return_exceptions=True)
            assert len(results) == 1 and isinstance(results[0], TimeoutError)
            assert fence.fenced and fence.processes[0].returncode is not None
            assert await worker.run_once()
            while await sender.run_once():
                pass
            operation = (await query(store, 'operation', accepted['operation_id']))['result']['operation']
            session = (await query(store, 'session', operation['resources']['session_id']))['result']['session']
            assert session['state'] == 'idle'
            assert session['attempts'][0]['state'] == 'failed'
            assert (await query(store, 'workspace', workspace_id))['result']['workspace']['active_attempt_ids'] == []
        finally:
            await worker.close()
            await fence.fence({})
            await store.close()
            await guest.close()
    asyncio.run(scenario())

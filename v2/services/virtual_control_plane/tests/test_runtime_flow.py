import asyncio
from copy import deepcopy

import pytest
from openengine.persistence import DatabaseConfig
from virtual_control_plane.application.runtime_updates import RuntimeUpdates
from virtual_control_plane.daemon.executor import FakeExecutor
from virtual_control_plane.daemon.journal import Journal
from virtual_control_plane.daemon.outbox import UpdateOutbox
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.service import StoreOwner
from virtual_control_plane.tests.test_execution import commands, execute, execute_request, query, workspace
from virtual_control_plane.transport.nats.runtime_protocol import Protocol
from virtual_control_plane.transport.nats.validation import Contracts
from virtual_control_plane.workers.runtime_commands import RuntimeDispatcher


class LocalTransport:
    def __init__(self, handler, definition, lose=False):
        self.handler, self.definition, self.lose = handler, definition, lose
        self.wires = []
        self.protocol = Protocol()

    async def request(self, subject, wire, response_definition):
        self.wires.append(wire)
        message = self.protocol.decode(self.definition, wire)
        response = await self.handler(subject, message)
        self.protocol.encode(response_definition, response)
        if self.lose:
            self.lose = False
            raise TimeoutError
        return response


async def setup(tmp_path):
    stores = []
    for name in ('vcp', 'daemon'):
        config = DatabaseConfig(sqlite_directory=tmp_path / name)
        PERSISTENCE.upgrade(config)
        stores.append(DurableStore(PERSISTENCE.database(config)))
    store, guest = stores
    workspace_id = await workspace(store)
    current = (await query(store, 'workspace', workspace_id))['result']['workspace']
    target = {'workspace_id': workspace_id, 'runtime_id': current['runtime']['runtime_id'], 'runtime_generation': 1}
    journal = Journal(guest, target, tmp_path)
    await journal.recover()
    updates = RuntimeUpdates(store)
    sender = UpdateOutbox(journal, LocalTransport(updates.accept, 'update'))
    assert await sender.run_once()
    return store, guest, workspace_id, journal, updates, sender


@pytest.mark.parametrize('outcome', ['completed', 'failed'])
def test_lost_acks_duplicate_delivery_and_public_projection(tmp_path, outcome):
    async def scenario():
        store, guest, workspace_id, journal, updates, sender = await setup(tmp_path)
        executor = FakeExecutor(journal)
        try:
            body = execute_request()
            body['payload']['agent']['settings'] = {'outcome': outcome, 'delay_seconds': 0}
            accepted = await execute(commands(store), workspace_id, body)
            transport = LocalTransport(journal.accept, 'execute', lose=True)
            dispatcher = RuntimeDispatcher(store, transport)
            assert not await dispatcher.run_once()
            # VCP restart forgets retry timers, but not command bytes/identity.
            dispatcher = RuntimeDispatcher(store, transport)
            assert await dispatcher.run_once()
            assert transport.wires[0] == transport.wires[1]
            assert await executor.run_once()
            await asyncio.gather(*executor.tasks)
            assert not await executor.run_once()
            sender.transport.lose = True
            assert not await sender.run_once()
            sender = UpdateOutbox(journal, sender.transport)
            while await sender.run_once():
                pass
            operation = (await query(store, 'operation', accepted['operation_id']))['result']['operation']
            assert operation['status'] == 'succeeded'
            session = (await query(store, 'session', operation['resources']['session_id']))['result']['session']
            assert session['state'] == 'idle'
            assert session['attempts'][0]['state'] == outcome
            async with guest.transaction() as tx:
                attempts = await tx.all('daemon_attempts')
                assert len(attempts) == 1 and attempts[0]['launch_count'] == 1
                entries = await tx.all('daemon_updates')
            async with store.transaction() as tx:
                outgoing = [row['event'] for row in await tx.all('outbox') if 'attempt_id' in row['event']['resources']]
                assert len(outgoing) == 3
                assert len(await tx.all('session_history')) == 1
                assert len(await tx.all('runtime_updates')) == 4
            contracts = Contracts()
            for event in outgoing:
                contracts.validate('events', event['event_type'].replace('.', '_') + '_event', event)
            for entry in entries:
                response = await updates.accept(f"oe.internal.v1.vcp.runtimes.{journal.target['runtime_id']}.updates", entry['message'])
                assert response['status'] == 'committed'
            async with store.transaction() as tx:
                assert len(await tx.all('outbox')) == 5  # two workspace events, three execution events
        finally:
            await executor.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['recorded', 'launching', 'running'])
def test_daemon_recovery_never_relaunches_uncertain_attempt(tmp_path, stage):
    async def scenario():
        store, guest, workspace_id, journal, updates, sender = await setup(tmp_path)
        executor = None
        try:
            accepted = await execute(commands(store), workspace_id)
            dispatcher = RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute'))
            assert await dispatcher.run_once()
            async with guest.transaction() as tx:
                attempt = (await tx.all('daemon_attempts'))[0]
                attempt['state'] = stage
                attempt['launch_count'] = 0 if stage == 'recorded' else 1
                if stage == 'running':
                    attempt['last_event_sequence'] = await journal.append(tx, 'attempt.started', {}, attempt['command'])
                await tx.put('daemon_attempts', accepted['resource_id'], attempt)
            journal = Journal(guest, journal.target, tmp_path)
            await journal.recover()
            sender = UpdateOutbox(journal, LocalTransport(updates.accept, 'update'))
            while await sender.run_once():
                pass
            executor = FakeExecutor(journal)
            launched = await executor.run_once()
            assert launched is (stage == 'recorded')
            await asyncio.gather(*executor.tasks)
            while await sender.run_once():
                pass
            async with guest.transaction() as tx:
                attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                assert attempt['launch_count'] == 1
                assert attempt['state'] == ('completed' if stage == 'recorded' else 'failed')
            operation = (await query(store, 'operation', accepted['operation_id']))['result']['operation']
            assert operation['status'] == ('failed' if stage == 'launching' else 'succeeded')
        finally:
            if executor:
                await executor.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_update_gaps_conflicts_and_wrong_bindings(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, updates, sender = await setup(tmp_path)
        executor = FakeExecutor(journal)
        try:
            await execute(commands(store), workspace_id)
            assert await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            assert await executor.run_once()
            await asyncio.gather(*executor.tasks)
            async with guest.transaction() as tx:
                entries = sorted(await tx.all('daemon_updates'), key=lambda row: row['message']['sequence'])
            subject = f"oe.internal.v1.vcp.runtimes.{journal.target['runtime_id']}.updates"
            gap = await updates.accept(subject, entries[2]['message'])
            assert gap['error']['code'] == 'EVENT_GAP' and gap['expected_sequence'] == 2
            wrong = deepcopy(entries[1]['message'])
            wrong['target']['runtime_generation'] = 2
            assert (await updates.accept(subject, wrong))['error']['code'] == 'STALE_RUNTIME'
            wrong = deepcopy(entries[1]['message'])
            wrong['execution']['cause']['operation']['operation_id'] = 'op_wrong'
            assert (await updates.accept(subject, wrong))['error']['code'] == 'IDENTITY_MISMATCH'
            assert (await updates.accept(subject, entries[1]['message']))['status'] == 'committed'
            conflict = deepcopy(entries[1]['message'])
            conflict['boot_id'] = 'boot_changed'
            assert (await updates.accept(subject, conflict))['error']['code'] == 'EVENT_CONFLICT'
            async with store.transaction() as tx:
                assert (await tx.get('runtime_status', journal.target['runtime_id']))['quarantined']
                assert len(await tx.all('runtime_updates')) == 2
        finally:
            await executor.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_journal_ownership_and_wire_limits(tmp_path):
    owner = StoreOwner(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            StoreOwner(tmp_path)
    finally:
        owner.close()
    protocol = Protocol()
    with pytest.raises(ValueError, match='1 MiB'):
        protocol.decode('execute', b' ' * 1048577)
    with pytest.raises(ValueError):
        protocol.decode('execute', b'\xff')


def test_daemon_conflicts_binding_paths_and_capacity(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, updates, sender = await setup(tmp_path)
        try:
            accepted = await execute(commands(store), workspace_id)
            async with store.transaction() as tx:
                row = (await tx.all('runtime_outbox'))[0]
            command, subject = row['message'], row['subject']
            wrong = deepcopy(command)
            wrong['target']['runtime_generation'] += 1
            assert (await journal.accept(subject, wrong))['error']['code'] == 'STALE_RUNTIME'
            original = await journal.accept(subject, command)
            assert original['status'] == 'accepted'
            assert await journal.accept(subject, command) == original
            changed = deepcopy(command)
            changed['payload']['task']['instruction'] = 'changed'
            assert (await journal.accept(subject, changed))['error']['code'] == 'COMMAND_ID_CONFLICT'
            changed['command_id'] = changed['execution']['command_id'] = 'rtc_other'
            assert (await journal.accept(subject, changed))['error']['code'] == 'ATTEMPT_CONFLICT'
            changed['command_id'] = changed['execution']['command_id'] = 'rtc_busy'
            changed['target']['attempt_id'] = 'attempt_other'
            assert (await journal.accept(subject, changed))['error']['code'] == 'SESSION_BUSY'
            changed['command_id'] = changed['execution']['command_id'] = 'rtc_path'
            changed['target']['session_id'] = 'session_other'
            outside = tmp_path.parent / 'outside-context'
            outside.mkdir(exist_ok=True)
            (tmp_path / 'escape').symlink_to(outside, target_is_directory=True)
            changed['payload']['context']['working_directory'] = 'escape'
            assert (await journal.accept(subject, changed))['error']['code'] == 'INVALID_ARGUMENT'
            # Admission rejections remain stable after the environment changes.
            (tmp_path / 'escape').unlink()
            (tmp_path / 'escape').mkdir()
            assert (await journal.accept(subject, changed))['error']['code'] == 'INVALID_ARGUMENT'
            changed['command_id'] = changed['execution']['command_id'] = 'rtc_capacity'
            journal.max_pending = 1
            assert (await journal.accept(subject, changed))['error']['code'] == 'UNAVAILABLE'
            async with guest.transaction() as tx:
                assert await tx.get('daemon_commands', 'rtc_capacity') is None
                assert len(await tx.all('daemon_attempts')) == 1
                assert (await tx.get('daemon_attempts', accepted['resource_id']))['launch_count'] == 0
        finally:
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_gap_response_replays_retained_acknowledged_updates(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, updates, sender = await setup(tmp_path)
        executor = FakeExecutor(journal)
        try:
            await execute(commands(store), workspace_id)
            assert await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            assert await executor.run_once()
            await asyncio.gather(*executor.tasks)
            # Simulate a sender believing sequence 2 was acknowledged; receiver
            # only has sequence 1. EVENT_GAP must rewind retained journal entries.
            async with guest.transaction() as tx:
                row = await tx.get('daemon_updates', '2')
                row['acknowledged'] = True
                await tx.put('daemon_updates', '2', row)
            assert not await sender.run_once()
            sender = UpdateOutbox(journal, sender.transport)
            while await sender.run_once():
                pass
            async with store.transaction() as tx:
                assert len(await tx.all('runtime_updates')) == 4
            assert [sender.transport.protocol.decode('update', wire)['sequence'] for wire in sender.transport.wires] == [1, 3, 2, 3, 4]
        finally:
            await executor.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('provider_succeeds', [True, False])
def test_ensure_completion_preserves_concurrent_runtime_updates(tmp_path, provider_succeeds):
    from virtual_control_plane.tests.test_lifecycle import FakeProvider, accept, request
    from virtual_control_plane.workers.operations import OperationWorker

    async def scenario():
        store, guest, workspace_id, journal, updates, sender = await setup(tmp_path)
        executor = FakeExecutor(journal)
        inspecting, release = asyncio.Event(), asyncio.Event()

        class PausedProvider(FakeProvider):
            async def inspect(self, ownership):
                inspecting.set()
                await release.wait()
                return provider_succeeds

        worker_task = None
        try:
            accepted = await execute(commands(store), workspace_id)
            assert await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            ensured = await accept(commands(store), request('ensure_concurrent', {}), 'ensure', workspace_id)
            assert ensured['status'] == 'accepted'
            worker_task = asyncio.create_task(OperationWorker(store, {'fake': PausedProvider()}).run_once())
            await asyncio.wait_for(inspecting.wait(), timeout=5)
            assert await executor.run_once()
            await asyncio.gather(*executor.tasks)
            while await sender.run_once():
                pass
            committed = (await query(store, 'workspace', workspace_id))['result']['workspace']
            assert committed['version'] == 6
            assert committed['active_attempt_ids'] == []
            release.set()
            assert await worker_task
            assert (await query(store, 'workspace', workspace_id))['result']['workspace'] == committed
            ensure_operation = (await query(store, 'operation', ensured['operation_id']))['result']['operation']
            assert ensure_operation['status'] == ('succeeded' if provider_succeeds else 'failed')
            operation = (await query(store, 'operation', accepted['operation_id']))['result']['operation']
            session = (await query(store, 'session', operation['resources']['session_id']))['result']['session']
            assert session['attempts'][0]['state'] == 'completed'
            destroyed = await accept(commands(store), request('destroy_after_ensure', {
                'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}}), 'destroy', workspace_id)
            assert destroyed['status'] == 'accepted'
            assert await OperationWorker(store, {'fake': FakeProvider()}).run_once()
            assert (await query(store, 'operation', destroyed['operation_id']))['result']['operation']['status'] == 'succeeded'
        finally:
            release.set()
            if worker_task:
                await asyncio.gather(worker_task, return_exceptions=True)
            await executor.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('failed_update', ['attempt.started', 'attempt.progress', 'attempt.completed'])
def test_executor_write_failure_is_reconciled_without_restart_or_relaunch(tmp_path, failed_update):
    async def scenario():
        store, guest, workspace_id, journal, updates, sender = await setup(tmp_path)
        executor = FakeExecutor(journal)
        try:
            accepted = await execute(commands(store), workspace_id)
            transport = LocalTransport(journal.accept, 'execute')
            assert await RuntimeDispatcher(store, transport).run_once()
            original_append = journal.append
            failures = [failed_update, 'attempt.failed']

            async def fail_after_write(tx, kind, payload, command=None, termination=None, cancel=None):
                sequence = await original_append(tx, kind, payload, command, termination, cancel)
                if failures and kind == failures[0]:
                    failures.pop(0)
                    message = 'Injected journal write failure'
                    raise RuntimeError(message)
                return sequence

            journal.append = fail_after_write
            assert await executor.run_once()
            results = await asyncio.gather(*executor.tasks, return_exceptions=True)
            assert len(results) == 1 and isinstance(results[0], RuntimeError)
            assert len(executor.tasks) == 1
            # Even a failure while recording the recovery outcome must retain
            # supervision and retry only the durable reconciliation.
            with pytest.raises(RuntimeError, match='Injected journal'):
                await executor.run_once()
            assert len(executor.tasks) == 1
            assert await executor.run_once()
            assert not executor.tasks
            assert not await executor.run_once()
            while await sender.run_once():
                pass
            operation = (await query(store, 'operation', accepted['operation_id']))['result']['operation']
            assert operation['status'] == ('failed' if failed_update == 'attempt.started' else 'succeeded')
            session = (await query(store, 'session', operation['resources']['session_id']))['result']['session']
            assert session['state'] == 'idle'
            assert session['attempts'][0]['state'] == 'failed'
            assert (await query(store, 'workspace', workspace_id))['result']['workspace']['active_attempt_ids'] == []
            async with guest.transaction() as tx:
                attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                assert attempt['state'] == 'failed' and attempt['launch_count'] == 1
                entries = await tx.all('daemon_updates')
                assert sum(row['message']['kind'] == 'attempt.failed' for row in entries) == 1
            replay = await journal.accept(f"oe.internal.v1.runtime.runtimes.{journal.target['runtime_id']}.execute",
                                          transport.protocol.decode('execute', transport.wires[0]))
            assert replay['status'] == 'accepted'
            assert not await executor.run_once()
        finally:
            await executor.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_runtime_registration_retries_until_provisioning_commits(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, updates, _ = await setup(tmp_path)
        try:
            async with store.transaction() as tx:
                record = await tx.get('workspace_records', workspace_id)
                record['workspace']['state'] = 'provisioning'
                runtime = record['workspace'].pop('runtime')
                await tx.put('workspace_records', workspace_id, record)
            await journal.recover()
            transport = LocalTransport(updates.accept, 'update')
            sender = UpdateOutbox(journal, transport)
            assert not await sender.run_once()
            async with guest.transaction() as tx:
                meta = await tx.get('daemon_meta', 'runtime')
                assert not meta.get('blocked')
                assert not meta['ready']
            async with store.transaction() as tx:
                record['workspace']['state'] = 'ready'
                record['workspace']['runtime'] = runtime
                await tx.put('workspace_records', workspace_id, record)
            # Restart drops only the retry timer; the update bytes stay stable.
            sender = UpdateOutbox(journal, transport)
            assert await sender.run_once()
            assert transport.wires[0] == transport.wires[1]
            async with guest.transaction() as tx:
                assert (await tx.get('daemon_meta', 'runtime'))['ready']
        finally:
            await store.close()
            await guest.close()
    asyncio.run(scenario())

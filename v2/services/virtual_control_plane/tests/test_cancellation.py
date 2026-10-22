import asyncio
from copy import deepcopy

import pytest
from virtual_control_plane.daemon.executor import ExecutionWorker
from virtual_control_plane.daemon.journal import Journal
from virtual_control_plane.daemon.outbox import UpdateOutbox
from virtual_control_plane.tests.test_execution import commands, execute, execute_request, query
from virtual_control_plane.tests.test_lifecycle import accept, request
from virtual_control_plane.tests.test_runtime_flow import LocalTransport, setup
from virtual_control_plane.transport.nats.validation import Contracts
from virtual_control_plane.workers.operations import OperationWorker
from virtual_control_plane.workers.runtime_commands import RuntimeDispatcher


async def cancel(store, workspace_id, attempt_id, command_id='cancel'):
    body = request(command_id, {'attempt_id': attempt_id, 'reason': 'User cancelled'})
    Contracts().validate('commands', 'cancel_command', body)
    response = await accept(commands(store), body, 'cancel', workspace_id)
    Contracts().validate('commands', 'attempt_command_response', response)
    return response


async def flush(sender):
    for _ in range(30):
        if not await sender.run_once():
            break


async def check_terminal(store, workspace_id, accepted, cancelled, state='cancelled'):
    operation = (await query(store, 'operation', accepted['operation_id']))['result']['operation']
    cancellation = (await query(store, 'operation', cancelled['operation_id']))['result']['operation']
    assert cancellation['status'] == 'succeeded'
    assert cancellation['result'] == {'attempt_id': accepted['resource_id'], 'final_state': state}
    session = (await query(store, 'session', operation['resources']['session_id']))['result']['session']
    assert session['attempts'][0]['state'] == state
    assert session['state'] == 'idle'
    current = (await query(store, 'workspace', workspace_id))['result']['workspace']
    assert current['active_attempt_ids'] == []
    async with store.transaction() as tx:
        events = [row['event'] for row in await tx.all('outbox')]
    terminal = [event for event in events if event['event_type'] in ('task.completed', 'task.failed', 'task.cancelled')]
    assert len(terminal) == 1 and terminal[0]['event_type'] == 'task.' + state
    for event in events:
        Contracts().validate('events', event['event_type'].replace('.', '_') + '_event', event)
    if state == 'cancelled':
        assert terminal[0]['payload']['cancelled_by']['operation_id'] == cancelled['operation_id']
        assert terminal[0]['payload']['operation']['operation_id'] == accepted['operation_id']
        assert terminal[0]['causal_path'][0] == 'cancel'
    return operation


@pytest.mark.parametrize('stage', ['before_execute', 'recorded', 'launching', 'running', 'completed'])
def test_cancel_races_and_retries(tmp_path, stage):
    async def scenario():
        store, guest, workspace_id, journal, updates, sender = await setup(tmp_path)
        worker = ExecutionWorker(journal)
        try:
            body = execute_request()
            body['payload']['agent']['settings'] = {'delay_seconds': 0 if stage == 'completed' else 30}
            accepted = await execute(commands(store), workspace_id, body)
            async with store.transaction() as tx:
                execution = (await tx.all('runtime_outbox'))[0]
            if stage != 'before_execute':
                assert await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            if stage in ('launching', 'running', 'completed'):
                assert await worker.run_once()
                if stage != 'launching':
                    await asyncio.sleep(.02)
                if stage == 'completed':
                    await asyncio.gather(*worker.tasks)
            cancelled = await cancel(store, workspace_id, accepted['resource_id'])
            assert await cancel(store, workspace_id, accepted['resource_id']) == cancelled
            other = await cancel(store, workspace_id, accepted['resource_id'], 'cancel_other')
            # Deliver cancels before execute deliberately; retry exact persisted bytes
            # after losing the durable acknowledgement.
            async with store.transaction() as tx:
                cancels = [row for row in await tx.all('runtime_outbox') if row['message']['kind'] == 'cancel']
            cancels.sort(key=lambda row: row['message']['cause']['operation']['command_id'])
            assert len(cancels) == 2
            transport = LocalTransport(journal.accept, 'cancel', lose=True)
            with pytest.raises(TimeoutError):
                await transport.request(cancels[0]['subject'], cancels[0]['wire'].encode(), 'command_ack')
            for entry in cancels:
                ack = await transport.request(entry['subject'], entry['wire'].encode(), 'command_ack')
                assert ack['status'] == 'accepted'
            assert transport.wires[0] == transport.wires[1]
            assert (await journal.accept(execution['subject'], execution['message']))['status'] == 'accepted'
            if stage in ('launching', 'running'):
                for _ in range(20):
                    await worker.run_once()
                    if not worker.tasks:
                        break
                    await asyncio.sleep(.01)
            else:
                await worker.run_once()
            sender.transport.lose = True
            assert not await sender.run_once()
            sender = UpdateOutbox(journal, sender.transport)
            await flush(sender)
            state = 'completed' if stage == 'completed' else 'cancelled'
            await check_terminal(store, workspace_id, accepted, cancelled, state)
            later = await cancel(store, workspace_id, accepted['resource_id'], 'cancel_later')
            for response in (other, later):
                op = (await query(store, 'operation', response['operation_id']))['result']['operation']
                assert op['result']['final_state'] == state
            async with guest.transaction() as tx:
                attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                assert attempt['launch_count'] == (1 if stage in ('launching', 'running', 'completed') else 0)
            # Restart daemon and redeliver both commands: no launch or new terminal event.
            journal = Journal(guest, journal.target, tmp_path)
            await journal.recover()
            await flush(UpdateOutbox(journal, sender.transport))
            assert (await journal.accept(execution['subject'], execution['message']))['status'] == 'accepted'
            assert (await journal.accept(cancels[0]['subject'], cancels[0]['message']))['status'] == 'accepted'
            await check_terminal(store, workspace_id, accepted, cancelled, state)
            instance = commands(store)
            destroyed = await accept(instance, request('destroy', {'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}}), 'destroy', workspace_id)
            assert destroyed['status'] == 'accepted'
            assert await OperationWorker(store, instance.providers).run_once()
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_cancel_validates_binding_and_failed_fence_retains_ownership(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, _, sender = await setup(tmp_path)
        worker = ExecutionWorker(journal)
        try:
            body = execute_request()
            body['payload']['agent']['settings'] = {'delay_seconds': 30}
            accepted = await execute(commands(store), workspace_id, body)
            assert (await cancel(store, workspace_id, 'attempt_missing', 'missing'))['error']['code'] == 'NOT_FOUND'
            await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            await worker.run_once()
            await asyncio.sleep(.01)
            cancelled = await cancel(store, workspace_id, accepted['resource_id'])
            await RuntimeDispatcher(store, LocalTransport(journal.accept, 'cancel')).run_once()
            backend = journal.backends['fake']
            original = backend.fence

            async def fail(handle):
                return False

            backend.fence = fail
            await worker.run_once()
            await asyncio.sleep(.01)
            with pytest.raises(ValueError, match='termination'):
                await worker.run_once()
            await flush(sender)
            current = (await query(store, 'workspace', workspace_id))['result']['workspace']
            assert current['active_attempt_ids'] == [accepted['resource_id']]
            op = (await query(store, 'operation', cancelled['operation_id']))['result']['operation']
            assert op['status'] == 'pending'
            destroy = await accept(commands(store), request('destroy', {}), 'destroy', workspace_id)
            assert destroy['status'] == 'rejected'
            backend.fence = original
            await worker.run_once()
            await flush(sender)
            await check_terminal(store, workspace_id, accepted, cancelled)
            async with store.transaction() as tx:
                row = next(row for row in await tx.all('runtime_outbox') if row['message']['kind'] == 'cancel')
            changed = deepcopy(row['message'])
            changed['command_id'] += '_different'
            changed['execution']['command_id'] += '_wrong'
            assert (await journal.accept(row['subject'], changed))['error']['code'] == 'ATTEMPT_CONFLICT'
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['cancel', 'ignore_cancel', 'stop_reading'])
def test_acp_cancellation_is_bounded_and_confirms_process_exit(tmp_path, mode):
    from virtual_control_plane.application.runtime_context import RuntimeContext
    from virtual_control_plane.tests.test_codex_executor import prepare

    async def scenario():
        store, guest, workspace_id, journal, sender, fence = await prepare(tmp_path, mode)
        worker = ExecutionWorker(journal, cancel_grace=.1)
        try:
            body = execute_request()
            body['payload']['agent'] = {'runner': 'codex'}
            if mode == 'stop_reading':
                body['payload']['task']['inputs'] = {'large': 'x' * 400000}
            accepted = await execute(commands(store, RuntimeContext()), workspace_id, body)
            await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            await worker.run_once()
            for _ in range(200):
                async with guest.transaction() as tx:
                    attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                if attempt['state'] == 'running':
                    break
                await asyncio.sleep(.01)
            assert attempt['state'] == 'running'
            # Give the subprocess time to reach prompt handling / pipe backpressure.
            await asyncio.sleep(.05)
            cancelled = await cancel(store, workspace_id, accepted['resource_id'])
            await RuntimeDispatcher(store, LocalTransport(journal.accept, 'cancel')).run_once()
            async with asyncio.timeout(5):
                while worker.tasks:
                    await worker.run_once()
                    await asyncio.sleep(.01)
            assert fence.processes and all(process.returncode is not None for process in fence.processes)
            await flush(sender)
            await check_terminal(store, workspace_id, accepted, cancelled)
            if mode == 'cancel':
                assert 'session/cancel' in (tmp_path / 'acp.log').read_text()
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('launched', [False, True])
def test_restart_with_durable_cancel_intent_and_failed_fencing(tmp_path, launched):
    async def scenario():
        store, guest, workspace_id, journal, _, sender = await setup(tmp_path)
        try:
            accepted = await execute(commands(store), workspace_id)
            await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            if launched:
                # Persisted launch intent is deliberately uncertain. Recovery
                # must fence it, never infer that the executor was never started.
                async with guest.transaction() as tx:
                    attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                    attempt.update(state='launching', executor_handle={'kind': 'in_process'}, launch_count=1)
                    await tx.put('daemon_attempts', accepted['resource_id'], attempt)
            cancelled = await cancel(store, workspace_id, accepted['resource_id'])
            await RuntimeDispatcher(store, LocalTransport(journal.accept, 'cancel', lose=True)).run_once()
            recovered = Journal(guest, journal.target, tmp_path)

            if launched:
                original = recovered.backends['fake'].fence

                async def fail(handle):
                    return False

                recovered.backends['fake'].fence = fail
                with pytest.raises(ValueError, match='Cannot fence'):
                    await recovered.recover()
                async with guest.transaction() as tx:
                    assert (await tx.get('daemon_attempts', accepted['resource_id']))['state'] == 'cancelling'
                recovered.backends['fake'].fence = original
            await recovered.recover()
            # Restart VCP's dispatcher too; lost acknowledgement resends the
            # same command without another terminal event.
            assert await RuntimeDispatcher(store, LocalTransport(recovered.accept, 'cancel')).run_once()
            await flush(UpdateOutbox(recovered, sender.transport))
            await check_terminal(store, workspace_id, accepted, cancelled)
            async with guest.transaction() as tx:
                attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                assert attempt['launch_count'] == int(launched)
        finally:
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_cancel_acceptance_rolls_back_if_receipt_write_fails(tmp_path):
    from contextlib import asynccontextmanager

    async def scenario():
        store, guest, workspace_id, _, _, _ = await setup(tmp_path)
        try:
            accepted = await execute(commands(store), workspace_id)

            class FaultyStore:
                @asynccontextmanager
                async def transaction(self):
                    async with store.transaction() as tx:
                        put = tx.put

                        async def fail(collection, key, value):
                            if collection == 'command_receipts':
                                message = 'Receipt write failed'
                                raise OSError(message)
                            await put(collection, key, value)

                        tx.put = fail
                        yield tx

            with pytest.raises(OSError, match='Receipt'):
                await cancel(FaultyStore(), workspace_id, accepted['resource_id'])
            async with store.transaction() as tx:
                assert len(await tx.all('runtime_outbox')) == 1
                assert (await tx.get('attempts', accepted['resource_id']))['attempt']['state'] == 'starting'
                assert all(row['operation']['operation'] != 'workspaces.cancel' for row in await tx.all('operations'))
            assert (await cancel(store, workspace_id, accepted['resource_id']))['status'] == 'accepted'
        finally:
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_cancel_after_lost_execute_rejection_has_one_terminal_outcome(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, _, sender = await setup(tmp_path)
        try:
            accepted = await execute(commands(store), workspace_id)
            journal.backends = {}
            # Daemon durably rejected execute, but VCP has not received that fact.
            assert not await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute', lose=True)).run_once()
            cancelled = await cancel(store, workspace_id, accepted['resource_id'])
            async with store.transaction() as tx:
                outgoing = next(row for row in await tx.all('runtime_outbox') if row['message']['kind'] == 'cancel')
            assert (await journal.accept(outgoing['subject'], outgoing['message']))['status'] == 'accepted'

            class Transport(LocalTransport):
                async def request(self, subject, wire, response_definition):
                    self.definition = subject.rsplit('.', 1)[1]
                    return await super().request(subject, wire, response_definition)

            dispatcher = RuntimeDispatcher(store, Transport(journal.accept, 'execute'))
            while await dispatcher.run_once():
                pass
            await flush(sender)
            await check_terminal(store, workspace_id, accepted, cancelled, 'failed')
        finally:
            await store.close()
            await guest.close()
    asyncio.run(scenario())

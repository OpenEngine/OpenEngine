import asyncio
from copy import deepcopy

import pytest
from virtual_control_plane.application.auth import LocalAccount
from virtual_control_plane.application.queries.read import Queries
from virtual_control_plane.daemon.executor import ExecutionWorker
from virtual_control_plane.daemon.journal import Journal
from virtual_control_plane.daemon.outbox import UpdateOutbox
from virtual_control_plane.daemon.steering import SteeringWorker
from virtual_control_plane.tests.test_cancellation import cancel, flush
from virtual_control_plane.tests.test_execution import commands, execute, execute_request, query
from virtual_control_plane.tests.test_lifecycle import accept, request
from virtual_control_plane.tests.test_runtime_flow import LocalTransport, setup
from virtual_control_plane.transport.nats.validation import Contracts
from virtual_control_plane.workers.runtime_commands import RuntimeDispatcher


async def steer(store, workspace_id, session_id, identifier='steer'):
    body = request(identifier, {'session_id': session_id, 'message': 'Focus on correctness'})
    Contracts().validate('commands', 'steer_command', body)
    response = await accept(commands(store), body, 'steer', workspace_id)
    Contracts().validate('commands', 'session_command_response', response)
    return response


async def running(tmp_path):
    store, guest, workspace_id, journal, updates, sender = await setup(tmp_path)
    body = execute_request()
    body['payload']['agent']['settings'] = {'delay_seconds': 30}
    execution = await execute(commands(store), workspace_id, body)
    await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
    worker = ExecutionWorker(journal)
    await worker.run_once()
    for _ in range(100):
        await flush(sender)
        operation = (await query(store, 'operation', execution['operation_id']))['result']['operation']
        if operation['status'] == 'succeeded':
            break
        await asyncio.sleep(.01)
    assert operation['status'] == 'succeeded'
    return store, guest, workspace_id, journal, updates, sender, worker, execution, operation['resources']['session_id']


async def history(store, session_id, params=None):
    result = await Queries(store, {}, LocalAccount()).get('', 'session_events', session_id, {'params': params or {}}, 'local')
    Contracts().validate('queries', 'list_session_events_response', result)
    return result


async def check(store, response, session_id, state):
    operation = (await query(store, 'operation', response['operation_id']))['result']['operation']
    assert operation['status'] == ('succeeded' if state == 'applied' else 'failed')
    entries = (await history(store, session_id, {'kind': ['steering']}))['result']['items']
    matching = [entry for entry in entries if entry['operation_id'] == response['operation_id']]
    assert len(matching) == 1 and matching[0]['status'] == state
    async with store.transaction() as tx:
        events = [row['event'] for row in await tx.all('outbox')
                  if row['event']['payload'].get('operation', {}).get('operation_id') == response['operation_id']]
    assert len(events) == 1 and events[0]['event_type'] == 'steering.' + state
    Contracts().validate('events', 'steering_' + state + '_event', events[0])
    return operation


def test_steering_duplicate_delivery_lost_acks_and_history(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, _, sender, worker, execution, session_id = await running(tmp_path)
        try:
            response = await steer(store, workspace_id, session_id)
            assert await steer(store, workspace_id, session_id) == response
            transport = LocalTransport(journal.accept, 'steer', lose=True)
            assert not await RuntimeDispatcher(store, transport).run_once()
            assert await RuntimeDispatcher(store, transport).run_once()
            assert transport.wires[0] == transport.wires[1]
            op = (await query(store, 'operation', response['operation_id']))['result']['operation']
            assert op['status'] == 'pending'  # durable ACK is not application
            assert await SteeringWorker(journal).run_once()
            assert not await SteeringWorker(journal).run_once()
            sender.transport.lose = True
            assert not await sender.run_once()
            await flush(UpdateOutbox(journal, sender.transport))
            await check(store, response, session_id, 'applied')
            async with guest.transaction() as tx:
                attempt = await tx.get('daemon_attempts', execution['resource_id'])
                assert list(attempt['steering'].values()) == ['Focus on correctness']
            again = await steer(store, workspace_id, session_id, 'steer2')
            await RuntimeDispatcher(store, LocalTransport(journal.accept, 'steer')).run_once()
            await SteeringWorker(journal).run_once()
            await flush(UpdateOutbox(journal, sender.transport))
            await check(store, again, session_id, 'applied')
            page = await history(store, session_id, {'kind': ['steering'], 'limit': 1})
            cursor = page['result']['next_cursor']
            assert cursor
            assert len((await history(store, session_id, {'kind': ['steering'], 'cursor': cursor}))['result']['items']) == 1
            assert (await history(store, session_id, {'cursor': cursor}))['error']['code'] == 'INVALID_CURSOR'
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('race', ['completion', 'cancel', 'restart', 'stale'])
def test_steering_races_never_retarget_or_duplicate(tmp_path, race):
    async def scenario():
        store, guest, workspace_id, journal, _, sender, worker, execution, session_id = await running(tmp_path)
        try:
            response = await steer(store, workspace_id, session_id)
            async with store.transaction() as tx:
                row = next(row for row in await tx.all('runtime_outbox') if row['message']['kind'] == 'steer')
            if race == 'stale':
                changed = deepcopy(row['message'])
                changed['target']['runtime_generation'] += 1
                assert (await journal.accept(row['subject'], changed))['error']['code'] == 'STALE_RUNTIME'
                async with guest.transaction() as tx:
                    assert not await tx.get('daemon_commands', changed['command_id'])
                await RuntimeDispatcher(store, LocalTransport(journal.accept, 'steer')).run_once()
                await SteeringWorker(journal).run_once()
                expected = 'applied'
            else:
                if race == 'completion':
                    # Serialize a completed executor outcome before steer arrives.
                    await worker.close()
                    async with guest.transaction() as tx:
                        attempt = await tx.get('daemon_attempts', execution['resource_id'])
                        await journal.finish(tx, attempt, 'completed', {'result': {'summary': 'Done'}})
                elif race == 'cancel':
                    await cancel(store, workspace_id, execution['resource_id'])
                    async with store.transaction() as tx:
                        cancellation = next(row for row in await tx.all('runtime_outbox') if row['message']['kind'] == 'cancel')
                    await journal.accept(cancellation['subject'], cancellation['message'])
                else:
                    # Lost ACK with pending steering; daemon restart fences the
                    # old execution. Pending steering must fail, never relaunch.
                    assert not await RuntimeDispatcher(store, LocalTransport(journal.accept, 'steer', lose=True)).run_once()
                    await worker.close()
                    journal = Journal(guest, journal.target, tmp_path)
                    await journal.recover()
                    await flush(UpdateOutbox(journal, sender.transport))
                await journal.accept(row['subject'], row['message'])
                await SteeringWorker(journal).run_once()
                expected = 'failed'
            await flush(UpdateOutbox(journal, sender.transport))
            await check(store, response, session_id, expected)
            assert (await journal.accept(row['subject'], row['message']))['status'] == 'accepted'
            assert not await SteeringWorker(journal).run_once()
            await flush(UpdateOutbox(journal, sender.transport))
            await check(store, response, session_id, expected)
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_unsupported_executor_is_rejected_before_acceptance(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, _, _, worker, _, session_id = await running(tmp_path)
        try:
            async with store.transaction() as tx:
                status = await tx.get('runtime_status', journal.target['runtime_id'])
                status['steering_executors'] = []
                await tx.put('runtime_status', journal.target['runtime_id'], status)
            response = await steer(store, workspace_id, session_id)
            assert response['status'] == 'rejected' and response['error']['code'] == 'UNSUPPORTED_CAPABILITY'
            async with store.transaction() as tx:
                assert not any(row['message']['kind'] == 'steer' for row in await tx.all('runtime_outbox'))
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_steering_effect_and_outcome_roll_back_together(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, _, sender, worker, execution, session_id = await running(tmp_path)
        try:
            response = await steer(store, workspace_id, session_id)
            await RuntimeDispatcher(store, LocalTransport(journal.accept, 'steer')).run_once()
            original = journal.append

            async def fail(*args, **kwargs):
                await original(*args, **kwargs)
                message = 'Injected outcome write failure'
                raise OSError(message)

            journal.append = fail
            with pytest.raises(OSError, match='Injected'):
                await SteeringWorker(journal).run_once()
            async with guest.transaction() as tx:
                assert not (await tx.get('daemon_attempts', execution['resource_id'])).get('steering')
                rows = [row for row in await tx.all('daemon_commands') if row.get('state') == 'steer_pending']
                assert len(rows) == 1
            journal.append = original
            await SteeringWorker(journal).run_once()
            await flush(sender)
            await check(store, response, session_id, 'applied')
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('state', ['starting', 'cancelling', 'idle', 'closed', 'stale', 'suspended'])
def test_steering_admission_states(tmp_path, state):
    async def scenario():
        store, guest, workspace_id, _, _, _, worker, execution, session_id = await running(tmp_path)
        try:
            async with store.transaction() as tx:
                if state == 'suspended':
                    record = await tx.get('workspace_records', workspace_id)
                    record['workspace']['state'] = 'suspended'
                    await tx.put('workspace_records', workspace_id, record)
                elif state in ('starting', 'cancelling'):
                    record = await tx.get('attempts', execution['resource_id'])
                    record['attempt']['state'] = state
                    await tx.put('attempts', execution['resource_id'], record)
                else:
                    record = await tx.get('sessions', session_id)
                    if state == 'stale':
                        record['session']['runtime_generation'] += 1
                    else:
                        record['session']['state'] = state
                    await tx.put('sessions', session_id, record)
            response = await steer(store, workspace_id, session_id)
            assert response['status'] == 'rejected'
            assert response['error']['code'] == ('UNSUPPORTED_CAPABILITY' if state in ('idle', 'suspended') else 'INVALID_STATE')
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())


def test_ready_without_steering_capability_revokes_previous_advertisement(tmp_path):
    async def scenario():
        store, guest, workspace_id, journal, updates, _, worker, _, session_id = await running(tmp_path)
        try:
            async with store.transaction() as tx:
                status = await tx.get('runtime_status', journal.target['runtime_id'])
            response = await updates.accept(f"oe.internal.v1.vcp.runtimes.{journal.target['runtime_id']}.updates",
                {'event_id': 'rte_legacy_ready', 'kind': 'runtime.ready', 'target': journal.target,
                 'boot_id': 'boot_legacy', 'sequence': status['sequence'] + 1, 'occurred_at': '2026-10-22T12:00:00Z',
                 'payload': {'journal_id': status['journal_id'], 'executors': ['fake'],
                             'progress_kinds': ['conversation'], 'max_concurrent_attempts': 4}})
            assert response['status'] == 'committed'
            assert (await steer(store, workspace_id, session_id))['error']['code'] == 'UNSUPPORTED_CAPABILITY'
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())

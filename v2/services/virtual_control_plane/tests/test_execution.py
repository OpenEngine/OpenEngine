import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json

import pytest
from openengine.persistence import DatabaseConfig
from virtual_control_plane.application.auth import LocalAccount
from virtual_control_plane.application.commands.lifecycle import Commands
from virtual_control_plane.application.queries.read import Queries
from virtual_control_plane.domain.execution import new_attempt, new_session, transition
from virtual_control_plane.domain.lifecycle import Rejected, now
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.tests.fake_runtime import FakeExecutionContext
from virtual_control_plane.tests.test_lifecycle import FakeProvider, accept, request
from virtual_control_plane.transport.nats.runtime_commands import RuntimeCommands
from virtual_control_plane.transport.nats.validation import Contracts
from virtual_control_plane.workers.operations import OperationWorker


@pytest.fixture
def config(tmp_path):
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    return config


def commands(store, context=None):
    return Commands(store, {'fake': FakeProvider()}, LocalAccount(),
                    context or FakeExecutionContext(), RuntimeCommands())


async def workspace(store):
    instance = commands(store)
    response = await accept(instance)
    await OperationWorker(store, instance.providers).run_once()
    return response['resource_id']


def execute_request(identifier='execute', session_id=None):
    selection = {'mode': 'new'} if session_id is None else {'mode': 'continue', 'session_id': session_id}
    return request(identifier, {'agent': {'runner': 'fake'}, 'task': {'instruction': 'Run the tests'}, 'session': selection})


async def execute(instance, workspace_id, body=None):
    body = body or execute_request()
    Contracts().validate('commands', 'execute_command', body)
    response = await accept(instance, body, 'execute', workspace_id)
    Contracts().validate('commands', 'attempt_command_response', response)
    return response


async def query(store, kind, target, params=None):
    result = await Queries(store, {}, LocalAccount()).get('', kind, target, {'params': params or {}}, 'local')
    definition = {'session': 'get_session_response', 'sessions': 'list_sessions_response',
                  'operation': 'get_operation_response', 'workspace': 'get_workspace_response'}[kind]
    Contracts().validate('queries', definition, result)
    return result


def test_execute_survives_restart_with_one_logical_runtime_command(config):
    async def scenario():
        store = DurableStore(PERSISTENCE.database(config))
        workspace_id = await workspace(store)
        response = await execute(commands(store), workspace_id)
        assert response['status'] == 'accepted'
        async with store.transaction() as tx:
            original = await tx.all('runtime_outbox')
            assert len(original) == 1
            assert original[0]['acknowledged'] is False
            assert json.loads(original[0]['wire']) == original[0]['message']
            assert RuntimeCommands().encode_execute(original[0]['message']) == original[0]['wire'].encode('utf-8')
            binding = await tx.get('attempt_bindings', response['resource_id'])
            assert binding['command_id'] == original[0]['message']['command_id']
        await store.close()
        store = DurableStore(PERSISTENCE.database(config))
        try:
            assert await execute(commands(store), workspace_id) == response
            assert not await OperationWorker(store, {'fake': FakeProvider()}).run_once()
            operation = (await query(store, 'operation', response['operation_id']))['result']['operation']
            assert operation['status'] == 'pending'
            session_id = operation['resources']['session_id']
            session = (await query(store, 'session', session_id))['result']['session']
            assert session['state'] == 'active'
            assert len(session['attempts']) == 1
            assert session['attempts'][0]['state'] == 'starting'
            assert session['attempts'][0]['attempt_id'] == response['resource_id']
            assert (await query(store, 'sessions', workspace_id))['result']['items'] == [session]
            current = (await query(store, 'workspace', workspace_id))['result']['workspace']
            assert current['active_attempt_ids'] == [response['resource_id']]
            async with store.transaction() as tx:
                assert await tx.all('runtime_outbox') == original
                assert len(await tx.all('sessions')) == len(await tx.all('attempts')) == 1
            conflict = execute_request()
            conflict['payload']['task']['instruction'] = 'Different task'
            assert (await execute(commands(store), workspace_id, conflict))['error']['code'] == 'COMMAND_ID_CONFLICT'
            destroy = await accept(commands(store), request('destroy', {'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}}), 'destroy', workspace_id)
            assert destroy['error']['code'] == 'INVALID_STATE'
        finally:
            await store.close()
    asyncio.run(scenario())


def test_concurrent_duplicate_commands_allocate_once(config):
    async def scenario():
        store = DurableStore(PERSISTENCE.database(config))
        workspace_id = await workspace(store)
        stores = [DurableStore(PERSISTENCE.database(config)) for _ in range(4)]
        try:
            replies = await asyncio.gather(*(execute(commands(item), workspace_id) for item in stores))
            assert all(reply == replies[0] for reply in replies)
            async with store.transaction() as tx:
                assert len(await tx.all('runtime_outbox')) == len(await tx.all('attempts')) == 1
        finally:
            for item in [store, *stores]:
                await item.close()
    asyncio.run(scenario())


def test_session_serialization_and_pagination(config):
    async def scenario():
        store = DurableStore(PERSISTENCE.database(config))
        try:
            workspace_id = await workspace(store)
            # Existing idle conversation, with no concurrent attempt at acceptance.
            session = new_session(workspace_id, 1, {'runner': 'fake'}, now())
            async with store.transaction() as tx:
                await tx.put('sessions', session['session_id'], {'owner': 'local', 'session': session})
            responses = await asyncio.gather(*(execute(commands(store), workspace_id, execute_request(f'continue_{i}', session['session_id'])) for i in range(2)))
            assert sorted(row['status'] for row in responses) == ['accepted', 'rejected']
            rejected = next(row for row in responses if row['status'] == 'rejected')
            assert rejected['error']['code'] == 'INVALID_STATE'
            await execute(commands(store), workspace_id, execute_request('new_session'))
            first = (await query(store, 'sessions', workspace_id, {'limit': 1}))['result']
            second = (await query(store, 'sessions', workspace_id, {'limit': 1, 'cursor': first['next_cursor']}))['result']
            assert first['items'][0]['session_id'] != second['items'][0]['session_id']
            assert second['next_cursor'] is None
            changed = await query(store, 'sessions', workspace_id, {'state': ['idle'], 'cursor': first['next_cursor']})
            assert changed['error']['code'] == 'INVALID_CURSOR'
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid', ['state', 'runtime', 'generation', 'ownership', 'session_generation', 'agent', 'context', 'unconfigured'])
def test_invalid_execution_creates_no_partial_records(config, invalid):
    class BadContext:
        async def prepare(self, *args):
            return {'working_directory': '../escape', 'instruction_files': [], 'references': []}

    async def scenario():
        store = DurableStore(PERSISTENCE.database(config))
        try:
            workspace_id = await workspace(store)
            body = execute_request()
            async with store.transaction() as tx:
                record = await tx.get('workspace_records', workspace_id)
                if invalid == 'state':
                    record['workspace']['state'] = 'suspended'
                elif invalid == 'runtime':
                    record['workspace']['runtime']['state'] = 'stopped'
                elif invalid == 'generation':
                    record['workspace']['runtime']['generation'] = 2
                elif invalid == 'ownership':
                    record['workspace']['runtime']['runtime_id'] = 'rt_other'
                elif invalid == 'session_generation':
                    session = new_session(workspace_id, 2, {'runner': 'fake'}, now())
                    await tx.put('sessions', session['session_id'], {'owner': 'local', 'session': session})
                    body = execute_request(session_id=session['session_id'])
                await tx.put('workspace_records', workspace_id, record)
            if invalid == 'agent':
                body['payload']['agent']['runner'] = 'unknown'
            instance = commands(store, BadContext() if invalid == 'context' else None)
            if invalid == 'unconfigured':
                instance.execution_context = None
            result = await execute(instance, workspace_id, body)
            assert result['status'] == 'rejected'
            assert await execute(instance, workspace_id, body) == result
            async with store.transaction() as tx:
                for collection in ('attempts', 'attempt_bindings', 'runtime_outbox'):
                    assert await tx.all(collection) == []
                assert (await tx.get('workspace_records', workspace_id))['workspace']['active_attempt_ids'] == []
                assert len(await tx.all('operations')) == 1  # Only the completed create.
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('fail_collection', ['sessions', 'attempts', 'attempt_bindings', 'runtime_outbox', 'operations', 'workspace_records', 'command_receipts'])
def test_execute_write_failure_rolls_back_every_record(config, fail_collection):
    class FailingStore:
        def __init__(self, store):
            self.store = store
        @asynccontextmanager
        async def transaction(self):
            async with self.store.transaction() as tx:
                class Fault:
                    get = tx.get
                    all = tx.all
                    async def put(self, collection, key, value):
                        if collection == fail_collection:
                            message = 'Injected durable write failure'
                            raise RuntimeError(message)
                        await tx.put(collection, key, value)
                yield Fault()

    async def scenario():
        store = DurableStore(PERSISTENCE.database(config))
        try:
            workspace_id = await workspace(store)
            async with store.transaction() as tx:
                before = await tx.get('workspace_records', workspace_id)
            with pytest.raises(RuntimeError, match='Injected'):
                await execute(commands(FailingStore(store)), workspace_id)
            async with store.transaction() as tx:
                assert await tx.get('workspace_records', workspace_id) == before
                assert await tx.get('command_receipts', 'execute') is None
                for collection in ('sessions', 'attempts', 'attempt_bindings', 'runtime_outbox'):
                    assert await tx.all(collection) == []
                assert len(await tx.all('operations')) == 1
        finally:
            await store.close()
    asyncio.run(scenario())


def test_explicit_session_and_attempt_transitions():
    timestamp = now()
    session = new_session('ws_test', 1, {'runner': 'fake'}, timestamp)
    attempt = new_attempt(session, 'task')
    original = deepcopy(attempt)
    running = transition(attempt, 'running', timestamp=timestamp)
    assert attempt == original
    assert running['started_at'] == timestamp
    completed_attempt = transition(running, 'completed', timestamp=timestamp)
    assert completed_attempt['ended_at'] == timestamp
    with pytest.raises(Rejected):
        transition(completed_attempt, 'running', timestamp=timestamp)
    with pytest.raises(Rejected):
        transition(attempt, 'completed', timestamp=timestamp)
    active = transition(session, 'active', timestamp=timestamp)
    with pytest.raises(Rejected):
        transition(active, 'active', timestamp=timestamp)
    assert transition(active, 'idle', timestamp=timestamp)['state'] == 'idle'

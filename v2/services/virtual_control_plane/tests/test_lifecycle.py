import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest
from openengine.persistence import DatabaseConfig
from virtual_control_plane.application.auth import LocalAccount
from virtual_control_plane.application.commands.lifecycle import Commands
from virtual_control_plane.application.queries.read import Queries
from virtual_control_plane.domain.lifecycle import Rejected, check_action
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.providers.smolvm.provider import CAPABILITIES
from virtual_control_plane.transport.nats.validation import Contracts
from virtual_control_plane.workers.operations import OperationWorker
from virtual_control_plane.workers.outbox import OutboxPublisher


class FakeProvider:
    capabilities = {**CAPABILITIES, 'provider': 'fake'}
    def __init__(self):
        self.runtimes = set()
        self.crash = False
        self.fail = False
    def validate(self, specification):
        pass
    async def provision(self, ownership, specification):
        if self.fail:
            message = 'provider failure'
            raise RuntimeError(message)
        self.runtimes.add(ownership['runtime_id'])
        if self.crash:
            self.crash = False
            raise asyncio.CancelledError
    async def inspect(self, ownership):
        return ownership['runtime_id'] in self.runtimes
    async def destroy(self, ownership, retention):
        self.runtimes.discard(ownership['runtime_id'])


class Memory:
    def __init__(self):
        self.data = {}
    @asynccontextmanager
    async def transaction(self):
        previous = deepcopy(self.data)
        try:
            yield self
        except BaseException:
            self.data = previous
            raise
    async def get(self, collection, key):
        return deepcopy(self.data.get(collection, {}).get(key))
    async def put(self, collection, key, value):
        self.data.setdefault(collection, {})[key] = deepcopy(value)
    async def all(self, collection):
        return deepcopy(list(self.data.get(collection, {}).values()))


def request(command_id='cmd_create', payload=None, **extra):
    return {'command_id': command_id, 'causal_path': [command_id], 'payload': payload if payload is not None else {'provider': 'fake'}, **extra}


async def accept(commands, body=None, action='create', target=None):
    subject = f'oe.v1.vcp.control.workspaces.{target + "." if target else ""}{action}'
    return await commands.accept(subject, action, target, body or request(), 'local')


@pytest.mark.parametrize('persistent', [False, True])
def test_lifecycle_duplicate_recovery_and_outbox(tmp_path, persistent):
    config = DatabaseConfig(sqlite_directory=tmp_path)
    if persistent:
        PERSISTENCE.upgrade(config)
    async def scenario():
        store = DurableStore(PERSISTENCE.database(config)) if persistent else Memory()
        provider = FakeProvider()
        providers = {'fake': provider}
        commands = Commands(store, providers, LocalAccount())
        response = await accept(commands)
        assert response == await accept(commands)
        conflict = await accept(commands, request(payload={'provider': 'other'}))
        assert conflict['error']['code'] == 'COMMAND_ID_CONFLICT'
        provider.crash = True
        with pytest.raises(asyncio.CancelledError):
            await OperationWorker(store, providers).run_once()
        if persistent:
            await store.close()
            store = DurableStore(PERSISTENCE.database(config))
            commands = Commands(store, providers, LocalAccount())
        assert await OperationWorker(store, providers).run_once()
        assert len(provider.runtimes) == 1
        queries = Queries(store, providers, LocalAccount())
        result = await queries.get('', 'workspace', response['resource_id'], {}, 'local')
        Contracts().validate('queries', 'get_workspace_response', result)
        assert result['result']['workspace']['state'] == 'ready'
        operation = await queries.get('', 'operation', response['operation_id'], {}, 'local')
        Contracts().validate('queries', 'get_operation_response', operation)
        assert operation['result']['operation']['status'] == 'succeeded'
        ensure = await accept(commands, request('cmd_ensure', {}), 'ensure', response['resource_id'])
        assert await OperationWorker(store, providers).run_once()
        assert (await queries.get('', 'workspace', response['resource_id'], {}, 'local')) == result
        ensure_result = await queries.get('', 'operation', ensure['operation_id'], {}, 'local')
        Contracts().validate('queries', 'get_operation_response', ensure_result)
        published = []
        class Publisher:
            async def publish(self, outgoing):
                published.append(outgoing)
                if len(published) == 1:
                    message = 'ack lost'
                    raise ConnectionError(message)
        publisher = OutboxPublisher(store, Publisher())
        with pytest.raises(ConnectionError):
            await publisher.run_once()
        while await publisher.run_once():
            pass
        assert published[0] == published[1]
        assert [item['resource_version'] for item in published] == [1, 1, 2]
        for item in published:
            Contracts().validate('events', item['event_type'].replace('.', '_') + '_event', item)
        destroy = await accept(commands, request('cmd_destroy', {'retention': {'artifacts': 'retain', 'checkpoints': 'delete'}}), 'destroy', response['resource_id'])
        assert await OperationWorker(store, providers).run_once()
        assert not provider.runtimes
        destruction = await queries.get('', 'operation', destroy['operation_id'], {}, 'local')
        Contracts().validate('queries', 'get_operation_response', destruction)
        assert (await queries.get('', 'workspaces', None, {}, 'local'))['result']['items'] == []
        if persistent:
            await store.close()
    asyncio.run(scenario())


def test_rejections_deadline_version_serialization_and_failure():
    async def scenario():
        store, provider = Memory(), FakeProvider()
        providers = {'fake': provider}
        commands = Commands(store, providers, LocalAccount())
        expired = request('old', deadline_at='2000-01-01T00:00:00Z')
        rejection = await accept(commands, expired)
        assert rejection['error']['code'] == 'DEADLINE_EXCEEDED'
        assert await accept(commands, request('old')) == rejection
        unsupported = await accept(commands, request('unsupported', {'provider': 'fake', 'required_capabilities': ['checkpoint']}))
        assert unsupported['error']['code'] == 'UNSUPPORTED_CAPABILITY'
        response = await accept(commands)
        busy = await accept(commands, request('ensure', {}), 'ensure', response['resource_id'])
        assert busy['error']['code'] == 'UNAVAILABLE'
        provider.fail = True
        await OperationWorker(store, providers).run_once()
        query = Queries(store, providers, LocalAccount())
        failed = await query.get('', 'operation', response['operation_id'], {}, 'local')
        Contracts().validate('queries', 'get_operation_response', failed)
        assert failed['result']['operation']['status'] == 'failed'
        stale = await accept(commands, request('stale', {'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}}, expected_version=1), 'destroy', response['resource_id'])
        assert stale['error']['code'] == 'VERSION_CONFLICT'
        for rejection in (rejection, unsupported, busy, stale):
            Contracts().validate('commands', 'workspace_command_response', rejection)
        denied = await commands.accept('subject', 'create', None, request('denied'), None)
        assert denied['error']['code'] == 'PERMISSION_DENIED'
        for row in await store.all('outbox'):
            event = row['event']
            Contracts().validate('events', event['event_type'].replace('.', '_') + '_event', event)
    asyncio.run(scenario())


def test_destroy_never_cancels_attempts_and_restore_is_explicit():
    workspace = {'workspace_id': 'ws_test', 'state': 'ready', 'provider': 'fake', 'active_attempt_ids': ['attempt_active']}
    with pytest.raises(Rejected, match='Cannot destroy'):
        check_action('destroy', workspace, {'retention': {'artifacts': 'retain', 'checkpoints': 'retain'}})
    workspace.update(state='suspended', active_attempt_ids=[])
    with pytest.raises(Rejected) as error:
        check_action('ensure', workspace, {})
    assert error.value.error['code'] == 'UNSUPPORTED_CAPABILITY'
    with pytest.raises(Rejected) as error:
        check_action('ensure', workspace, {'restore': 'forbid'})
    assert error.value.error['code'] == 'INVALID_STATE'


def test_pagination_survives_restart_and_rejects_changed_filters(tmp_path):
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    async def scenario():
        store = DurableStore(PERSISTENCE.database(config))
        providers = {'fake': FakeProvider()}
        commands = Commands(store, providers, LocalAccount())
        await asyncio.gather(*(accept(commands, request(f'cmd_{index}')) for index in range(5)))
        query = Queries(store, providers, LocalAccount())
        first = await query.get('', 'workspaces', None, {'params': {'limit': 2}}, 'local')
        cursor = first['result']['next_cursor']
        await store.close()
        store = DurableStore(PERSISTENCE.database(config))
        query = Queries(store, providers, LocalAccount())
        second = await query.get('', 'workspaces', None, {'params': {'limit': 2, 'cursor': cursor}}, 'local')
        assert len(second['result']['items']) == 2
        assert {row['workspace_id'] for row in first['result']['items']}.isdisjoint(row['workspace_id'] for row in second['result']['items'])
        invalid = await query.get('', 'workspaces', None, {'params': {'provider': 'fake', 'cursor': cursor}}, 'local')
        assert invalid['error']['code'] == 'INVALID_CURSOR'
        await store.close()
    asyncio.run(scenario())


def test_concurrent_duplicate_acceptance_has_one_operation(tmp_path):
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)
    async def scenario():
        stores = [DurableStore(PERSISTENCE.database(config)) for _ in range(8)]
        try:
            replies = await asyncio.gather(*(accept(Commands(store, {'fake': FakeProvider()}, LocalAccount())) for store in stores))
            assert all(reply == replies[0] for reply in replies)
            async with stores[0].transaction() as tx:
                assert len(await tx.all('operations')) == 1
                assert len(await tx.all('runtime_ownership')) == 1
        finally:
            for store in stores:
                await store.close()
    asyncio.run(scenario())


def test_store_owner_excludes_second_service_and_releases_on_close(tmp_path):
    from virtual_control_plane.service import StoreOwner
    first = StoreOwner(tmp_path)
    try:
        with pytest.raises(RuntimeError, match='Another VCP'):
            StoreOwner(tmp_path)
    finally:
        first.close()
    StoreOwner(tmp_path).close()


@pytest.mark.parametrize('action', ['create', 'ensure', 'destroy'])
@pytest.mark.parametrize('status', ['pending', 'running'])
def test_missing_provider_after_restart_fails_without_blocking_other_work(tmp_path, action, status):
    config = DatabaseConfig(sqlite_directory=tmp_path)
    PERSISTENCE.upgrade(config)

    async def scenario():
        store = DurableStore(PERSISTENCE.database(config))
        missing, available = FakeProvider(), FakeProvider()
        providers = {'fake': missing, 'other': available}
        commands = Commands(store, providers, LocalAccount())
        creation = await accept(commands)
        workspace_id = creation['resource_id']
        response = creation
        if action != 'create':
            await OperationWorker(store, providers).run_once()
            payload = {'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}} if action == 'destroy' else {}
            response = await accept(commands, request('cmd_action', payload), action, workspace_id)
        async with store.transaction() as tx:
            record = await tx.get('operations', response['operation_id'])
            record['operation']['status'] = status
            await tx.put('operations', response['operation_id'], record)
            original_ownership = await tx.get('runtime_ownership', workspace_id)
        other = await accept(commands, request('cmd_other', {'provider': 'other'}))
        await store.close()

        # Reopen the durable store with only the unrelated provider configured.
        store = DurableStore(PERSISTENCE.database(config))
        try:
            worker = OperationWorker(store, {'other': available})
            assert await worker.run_once()
            async with store.transaction() as tx:
                failed = (await tx.get('operations', response['operation_id']))['operation']
                assert failed['status'] == 'failed'
                assert failed['error']['code'] == 'PROVIDER_ERROR'
                assert "Provider 'fake' is not configured" in failed['error']['message']
                Contracts().validate('operations', 'operation', failed)
                workspace = await tx.get('workspace_records', workspace_id)
                assert 'active_operation' not in workspace
                assert workspace['workspace']['state'] == ('ready' if action == 'ensure' else 'failed')
                assert await tx.get('runtime_ownership', workspace_id) == original_ownership
            assert await worker.run_once()
            assert not await worker.run_once()
            async with store.transaction() as tx:
                assert (await tx.get('operations', other['operation_id']))['operation']['status'] == 'succeeded'
                for row in await tx.all('outbox'):
                    event = row['event']
                    Contracts().validate('events', event['event_type'].replace('.', '_') + '_event', event)
            # Restoring configuration permits explicit cleanup with a new ID.
            commands = Commands(store, providers, LocalAccount())
            cleanup = await accept(commands, request('cmd_cleanup', {'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}}), 'destroy', workspace_id)
            assert cleanup['status'] == 'accepted'
            assert await OperationWorker(store, providers).run_once()
            assert not missing.runtimes
        finally:
            await store.close()

    asyncio.run(scenario())

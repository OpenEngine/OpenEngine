import asyncio
import json

import pytest
import sqlalchemy as sa
from openengine.persistence import DatabaseConfig

from virtual_control_plane.daemon.initialization import Initialization
from virtual_control_plane.daemon.journal import Journal
from virtual_control_plane.daemon.service import parser, serve
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.persistence.schema import documents

TARGET = {'workspace_id': 'ws_one', 'runtime_id': 'rt_one', 'runtime_generation': 1}


def database(path):
    config = DatabaseConfig(sqlite_directory=path)
    PERSISTENCE.upgrade(config)
    return DurableStore(PERSISTENCE.database(config))


@pytest.mark.parametrize('phase', ['intent', 'migration', 'metadata'])
@pytest.mark.parametrize('initialize', [True, False])
def test_interrupted_first_setup_resumes_with_same_binding(tmp_path, phase, initialize):
    async def scenario():
        Initialization(tmp_path, TARGET, True)
        previous = None
        if phase != 'intent':
            store = database(tmp_path)
            try:
                if phase == 'metadata':
                    await Journal(store, TARGET, tmp_path).recover()
                    async with store.transaction() as tx:
                        previous = await tx.get('daemon_meta', 'runtime')
            finally:
                await store.close()
        resumed = Initialization(tmp_path, TARGET, initialize)
        store = database(tmp_path)
        try:
            await resumed.check(store)
            await Journal(store, TARGET, tmp_path).recover()
            resumed.finish()
            assert json.loads((tmp_path / 'binding.json').read_text()) == TARGET
            assert not (tmp_path / 'initializing.json').exists()
            async with store.transaction() as tx:
                meta = await tx.get('daemon_meta', 'runtime')
                if previous:
                    assert meta['journal_id'] == previous['journal_id']
                    assert meta['sequence'] > previous['sequence']
                assert not await tx.all('daemon_attempts')
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('initialize', [True, False])
def test_lost_established_metadata_is_never_reinitialized(tmp_path, initialize):
    async def scenario():
        initialization = Initialization(tmp_path, TARGET, True)
        store = database(tmp_path)
        try:
            await Journal(store, TARGET, tmp_path).recover()
            initialization.finish()
            # Even an old pending marker left after a crash cannot bypass this.
            (tmp_path / 'initializing.json').write_text(json.dumps(TARGET))
            async with store.database.connect() as connection:
                await connection.execute(sa.delete(documents['daemon_meta']))
                await connection.commit()
            restart = Initialization(tmp_path, TARGET, initialize)
            with pytest.raises(ValueError, match='Lost runtime journal'):
                await restart.check(store)
        finally:
            await store.close()
    asyncio.run(scenario())


def test_pending_initialization_rejects_other_generation(tmp_path):
    Initialization(tmp_path, TARGET, True)
    with pytest.raises(ValueError, match='binding changed'):
        Initialization(tmp_path, {**TARGET, 'runtime_generation': 2}, True)


def test_failed_executor_configuration_can_be_corrected(tmp_path):
    async def scenario():
        args = parser().parse_args(['--journal-directory', str(tmp_path), '--workspace-root', str(tmp_path),
                                   '--workspace-id', 'ws_one', '--runtime-id', 'rt_one', '--generation', '1',
                                   '--initialize', '--codex-acp', '/missing/adapter'])
        with pytest.raises(ValueError, match='Codex requires'):
            await serve(args)
        assert (tmp_path / 'initializing.json').exists()
        assert not (tmp_path / 'binding.json').exists()
        args.initialize = False
        args.codex_acp = None  # Correct deployment to the available fake executor.
        # Stop at the credentials check, after recovery and before any listener.
        with pytest.raises(ValueError, match='Provision credentials'):
            await serve(args)
        assert (tmp_path / 'binding.json').exists()
        assert not (tmp_path / 'initializing.json').exists()
        store = database(tmp_path)
        try:
            await Initialization(tmp_path, TARGET, False).check(store)
        finally:
            await store.close()
    asyncio.run(scenario())

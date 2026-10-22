"""Opt-in Linux guest checks. No live agent or credentials are installed by tests."""
import asyncio
import os
from pathlib import Path
import sys

import pytest
from virtual_control_plane.daemon.cgroups import CgroupFence
from virtual_control_plane.daemon.codex import CodexBackend
from virtual_control_plane.daemon.executor import ExecutionWorker
from virtual_control_plane.daemon.journal import Journal
from virtual_control_plane.daemon.outbox import UpdateOutbox
from virtual_control_plane.application.runtime_context import RuntimeContext
from virtual_control_plane.tests.test_execution import commands, execute, execute_request, query
from virtual_control_plane.tests.test_runtime_flow import LocalTransport, setup
from virtual_control_plane.workers.runtime_commands import RuntimeDispatcher

CONFIGURED = sys.platform == 'linux' and all(os.environ.get(key) for key in (
    'OE_VCP_TEST_CGROUP_ROOT', 'OE_VCP_TEST_AGENT_UID', 'OE_VCP_TEST_AGENT_GID', 'OE_VCP_TEST_AGENT_HOME'))
pytestmark = pytest.mark.skipif(not CONFIGURED, reason='Requires a provisioned Linux cgroup-v2 guest and agent account')


def fencing():
    return CgroupFence(os.environ['OE_VCP_TEST_CGROUP_ROOT'], uid=int(os.environ['OE_VCP_TEST_AGENT_UID']),
                       gid=int(os.environ['OE_VCP_TEST_AGENT_GID']), home=os.environ['OE_VCP_TEST_AGENT_HOME'])


def test_linux_fence_terminates_process_tree():
    async def scenario():
        fence = fencing()
        handle = fence.plan()
        process = await asyncio.create_subprocess_exec(*fence.command(handle, ['/bin/sh', '-c', 'sleep 60 & wait']))
        try:
            for _ in range(100):
                if 'populated 1' in (fence.path(handle) / 'cgroup.events').read_text():
                    break
                await asyncio.sleep(.01)
            assert 'populated 1' in (fence.path(handle) / 'cgroup.events').read_text()
            assert await fence.fence(handle)
            await asyncio.wait_for(process.wait(), timeout=5)
            assert not fence.path(handle).exists()
        finally:
            await fence.fence(handle)
            await process.wait()
    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get('OE_VCP_TEST_CODEX_ACP'), reason='Requires preinstalled, authenticated Codex ACP adapter')
@pytest.mark.parametrize('cancel_task', [False, True])
def test_live_codex_task(tmp_path, cancel_task):
    async def scenario():
        store, guest, workspace_id, original, updates, _ = await setup(tmp_path)
        backend = CodexBackend([os.environ['OE_VCP_TEST_CODEX_ACP']], fencing())
        # Provision a workspace readable by the unprivileged guest agent.
        root = Path(os.environ['OE_VCP_TEST_AGENT_HOME'])
        journal = Journal(guest, original.target, root, backends={'codex': backend})
        await journal.recover()
        sender = UpdateOutbox(journal, LocalTransport(updates.accept, 'update'))
        worker = ExecutionWorker(journal)
        try:
            while await sender.run_once():
                pass
            body = execute_request()
            body['payload']['agent'] = {'runner': 'codex'}
            body['payload']['task']['instruction'] = 'Reply with VCP_OK. Do not change files or run commands.'
            accepted = await execute(commands(store, RuntimeContext()), workspace_id, body)
            assert await RuntimeDispatcher(store, LocalTransport(journal.accept, 'execute')).run_once()
            assert await worker.run_once()
            if cancel_task:
                from virtual_control_plane.tests.test_cancellation import cancel, check_terminal
                for _ in range(1000):
                    async with guest.transaction() as tx:
                        attempt = await tx.get('daemon_attempts', accepted['resource_id'])
                    if attempt['state'] == 'running':
                        break
                    await asyncio.sleep(.01)
                assert attempt['state'] == 'running'
                cancelled = await cancel(store, workspace_id, accepted['resource_id'])
                await RuntimeDispatcher(store, LocalTransport(journal.accept, 'cancel')).run_once()
                async with asyncio.timeout(30):
                    while worker.tasks:
                        await worker.run_once()
                        await asyncio.sleep(.01)
                assert not backend.process_fence.path(attempt['executor_handle']).exists()
            else:
                await asyncio.wait_for(asyncio.gather(*worker.tasks), timeout=120)
            while await sender.run_once():
                pass
            operation = (await query(store, 'operation', accepted['operation_id']))['result']['operation']
            session = (await query(store, 'session', operation['resources']['session_id']))['result']['session']
            if cancel_task:
                from virtual_control_plane.tests.test_lifecycle import accept, request
                from virtual_control_plane.workers.operations import OperationWorker
                await check_terminal(store, workspace_id, accepted, cancelled)
                again = await cancel(store, workspace_id, accepted['resource_id'], 'cancel_again')
                assert (await query(store, 'operation', again['operation_id']))['result']['operation']['result']['final_state'] == 'cancelled'
                instance = commands(store)
                destroyed = await accept(instance, request('destroy', {'retention': {'artifacts': 'delete', 'checkpoints': 'delete'}}), 'destroy', workspace_id)
                assert destroyed['status'] == 'accepted'
                assert await OperationWorker(store, instance.providers).run_once()
            else:
                assert session['attempts'][0]['state'] == 'completed'
                assert 'VCP_OK' in session['attempts'][0]['result']['summary']
        finally:
            await worker.close()
            await store.close()
            await guest.close()
    asyncio.run(scenario())

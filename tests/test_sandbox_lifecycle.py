"""Node deadlines, cancellation ownership, and recovery after a killed host process."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry, StdioACPProvider

from engine.adapters.sandbox.smolvm import SmolvmError, SmolvmSandbox
from engine.adapters.sandbox.smolvm._lifecycle import LEASE, MANAGED, SCOPE
from engine.apps.web.composition import build_graph_runtime
from engine.graph_runtime import EventLog, GraphId
from engine.graph_runtime_langgraph import ACPNode, LangGraphDefinition, LangGraphRuntime
from engine.graph_service.compile import compile_graph
from engine.graph_service.language import GraphError, parse_graph
from engine.ports import SandboxSpec


@pytest.mark.parametrize('timeout', [0, -1, None, True, '30m', float('nan'), float('inf'), pytest.param(10 ** 400, id='overflow')])
def test_invalid_node_timeout_is_refused_in_python_and_yaml(timeout):
    with pytest.raises(ValueError, match='timeout'):
        ACPNode(agent='test', cwd='.', timeout=timeout)
    with pytest.raises(GraphError) as error:
        parse_graph({'apiVersion': 'openengine.cc/v1', 'name': 'timeout', 'implementation': {
            'task': {'agent': 'test', 'prompt': 'Work', 'timeout': timeout},
        }})
    assert any(problem.path == 'implementation.task.timeout' for problem in error.value.problems)


def test_yaml_timeout_reaches_each_parallel_node():
    spec = parse_graph({'apiVersion': 'openengine.cc/v1', 'name': 'timeout', 'review': {
        'facets': {'agent': 'test', 'prompt': 'Review', 'timeout': 42.5, 'facets': [{'facet': 'security'}, {'facet': 'bugs'}]},
    }})
    workflow = compile_graph(spec, version_id='timeout-v1', number=1, workspace_provider=object(), registry=ACPAgentRegistry([]))
    assert ACPNode(agent='test', cwd='.').timeout == 1800
    for key in spec.nodes[0].keys:
        assert workflow.builder.nodes[key].runnable.afunc.timeout == 42.5


@pytest.fixture(params=['fake', pytest.param('real', marks=pytest.mark.integration)])
def backend(request, tmp_path):
    if request.param == 'fake':
        executable, _ = request.getfixturevalue('smolvm_cli')
        return SmolvmSandbox('fixture', executable=str(executable), state_directory=tmp_path / 'state')
    image = os.environ.get('OE_SMOLVM_TEST_IMAGE')
    if not image:
        pytest.skip('set OE_SMOLVM_TEST_IMAGE for real VM lifecycle checks')
    return SmolvmSandbox(image, executable=os.environ.get('OE_SMOLVM_EXECUTABLE', 'smolvm'), state_directory=tmp_path / 'state')


async def listing(backend):
    from engine.adapters.sandbox.smolvm import SmolvmInstance
    instance = SmolvmInstance(backend.executable, 'inspection', SandboxSpec())
    return json.loads((await instance._command(['machine', 'ls', '--json']))[0])


def graph(node, backend):
    builder = StateGraph(dict)
    builder.add_node('agent', node)
    builder.add_edge(START, 'agent')
    builder.add_edge('agent', END)
    return LangGraphRuntime(LangGraphDefinition(graph_id=GraphId('lifecycle'), name='Lifecycle',
        graph=builder.compile(checkpointer=InMemorySaver())), sandbox=backend)


@pytest.mark.parametrize('mode', ['success', 'failure', 'timeout', 'approval-timeout', 'cancel', 'shutdown'])
def test_node_exit_paths_destroy_vm(backend, tmp_path, mode):
    source = tmp_path / 'source'
    source.mkdir()
    peer = Path(__file__).with_name('sandbox_acp_agent.py').read_text()
    if mode == 'approval-timeout':
        peer = peer.replace('        if "--hang-prompt" in sys.argv:', '''        print(json.dumps({"jsonrpc": "2.0", "id": "ask-permission", "method": "session/request_permission",
            "params": {"sessionId": session_id, "toolCall": {"toolCallId": "wait", "title": "Wait for approval", "kind": "execute", "rawInput": {"command": "true"}},
            "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}, {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}}), flush=True)
        sys.stdin.readline()
        if "--hang-prompt" in sys.argv:''')
    (source / 'agent.py').write_text(peer)
    (source / 'input.txt').write_text('workspace-marker')
    args = () if mode == 'success' else ('--exit-init',) if mode == 'failure' else ('--hang-prompt',)
    from engine.graph_runtime_langgraph.acp import answer_permission
    provider = StdioACPProvider(name='test', command=('python3', 'agent.py', *args), permissions=answer_permission)
    entered, names = None, []
    class Capture:
        sweep_orphans = backend.sweep_orphans
        @asynccontextmanager
        async def create(self, spec):
            async with backend.create(spec) as vm:
                names.append(vm.name)
                machines = await listing(backend)
                record = next(m for m in machines if m['name'] == vm.name)
                assert record['labels']['node'] == 'agent'
                assert record['labels']['run'].startswith('run-')
                assert record['labels'][SCOPE] == backend.ownership.scope
                entered.set()
                yield vm
    node = ACPNode(agent='test', registry=ACPAgentRegistry([provider]), cwd=str(source), prompt='Work',
        timeout=12 if mode in {'timeout', 'approval-timeout'} else 90)
    runtime = graph(node, Capture())
    events = EventLog()
    runtime.observe(events.append)
    async def scenario():
        nonlocal entered
        saw_approval = False
        entered = asyncio.Event()
        try:
            run = await runtime.start(GraphId('lifecycle'), {})
            if mode in {'cancel', 'shutdown'}:
                await asyncio.wait_for(entered.wait(), 60)
                if mode == 'cancel':
                    await runtime.cancel(run.run_id)
                else:
                    close = asyncio.create_task(runtime.aclose())
                    await asyncio.sleep(0)
                    close.cancel()
                    await asyncio.sleep(0)
                    close.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await close
                assert not runtime.executors(run.run_id)
            else:
                async with asyncio.timeout(100):
                    async for event in events.stream(run.run_id):
                        if event.kind.value == 'approval.requested':
                            saw_approval = True
                        if event.kind.value in {'run.finished', 'run.failed'}:
                            assert event.kind.value == ('run.finished' if mode == 'success' else 'run.failed'), event.payload
                            if mode in {'timeout', 'approval-timeout'}:
                                assert 'timed out after 12 seconds' in str(event.payload)
                            break
                if mode == 'approval-timeout':
                    assert saw_approval
                    assert not (await runtime.snapshot(run.run_id)).pending_approvals
        finally:
            await runtime.aclose()
        assert names
        assert not any(m['name'] in names for m in await listing(backend))
        assert not list(backend.ownership.directory.glob('*.lease'))
    asyncio.run(scenario())


def record_orphan(root, backend, *, scope=None, managed='1'):
    identity = uuid.uuid4().hex
    name = 'oe-sandbox-' + identity
    machine = root / name
    machine.mkdir()
    (machine / 'labels.json').write_text(json.dumps({MANAGED: managed, SCOPE: scope or backend.ownership.scope, LEASE: identity}))
    return name


def test_startup_sweep_removes_only_orphans_for_this_daemon(smolvm_cli, tmp_path):
    executable, root = smolvm_cli
    backend = SmolvmSandbox('fixture', executable=str(executable), state_directory=tmp_path / 'state')
    orphan = record_orphan(root, backend)
    other = record_orphan(root, backend, scope='another-daemon')
    unowned = record_orphan(root, backend, managed='no')
    async def scenario():
        async with backend.create(SandboxSpec(labels={MANAGED: 'forged', SCOPE: 'forged', LEASE: 'forged'})) as active:
            assert await backend.sweep_orphans() == (orphan,)
            assert {m['name'] for m in await listing(backend)} == {other, unowned, active.name}
            assert await backend.sweep_orphans() == ()
    asyncio.run(scenario())


def test_failed_sweep_prevents_runtime_startup_and_can_be_retried(smolvm_cli, tmp_path):
    executable, root = smolvm_cli
    backend = SmolvmSandbox('fixture', executable=str(executable), state_directory=tmp_path / 'state')
    name = record_orphan(root, backend)
    (root / 'fail-delete').touch()
    settings = SimpleNamespace(graph_state_directory=tmp_path / 'state', engine_config=SimpleNamespace(
        public_url='', sandbox=SimpleNamespace(backend='smolvm', image='fixture')))
    async def scenario():
        with pytest.raises(SmolvmError, match='could not delete'):
            async with build_graph_runtime(settings, []):
                pytest.fail('graph runtime was offered before recovery completed')
        assert (root / name).exists()
        (root / 'fail-delete').unlink()
        async with build_graph_runtime(settings, []):
            assert not (root / name).exists()
    asyncio.run(scenario())


def test_startup_is_once_even_for_concurrent_callers():
    class Backend:
        calls = 0
        async def sweep_orphans(self):
            self.calls += 1
            await asyncio.sleep(0)
            return ()
    backend = Backend()
    runtime = LangGraphRuntime(sandbox=backend)
    async def scenario():
        await asyncio.gather(runtime.startup(), runtime.startup(), runtime.startup())
        assert backend.calls == 1
        await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.integration
def test_killed_host_is_recovered_before_daemon_runtime_is_offered(tmp_path):
    image = os.environ.get('OE_SMOLVM_TEST_IMAGE')
    if not image:
        pytest.skip('set OE_SMOLVM_TEST_IMAGE for a real SIGKILL/restart recovery test')
    executable = os.environ.get('OE_SMOLVM_EXECUTABLE', 'smolvm')
    state, source, ready = tmp_path / 'state', tmp_path / 'source', tmp_path / 'ready.json'
    source.mkdir()
    peer = Path(__file__).with_name('sandbox_acp_agent.py').read_text().replace(
        '        if "--hang-prompt" in sys.argv:\n            time.sleep(120)',
        '        if "--hang-prompt" in sys.argv:\n            Path("node-ready").write_text("running")\n            time.sleep(120)')
    (source / 'agent.py').write_text(peer)
    (source / 'input.txt').write_text('workspace-marker')
    backend = SmolvmSandbox(image, executable=executable, state_directory=state)
    async def scenario():
        child = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name('sandbox_lifecycle_driver.py')),
            image, executable, str(state), str(source), str(ready), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            async with asyncio.timeout(100):
                while not ready.exists():
                    if child.returncode is not None:
                        pytest.fail((await child.stderr.read()).decode())
                    await asyncio.sleep(0.05)
            name = json.loads(ready.read_text())['name']
            child.kill()  # SIGKILL: no Python finally/context-manager cleanup runs.
            await child.wait()
            assert next(m for m in await listing(backend) if m['name'] == name)['state'] == 'running'
            settings = SimpleNamespace(graph_state_directory=state, engine_config=SimpleNamespace(
                public_url='', sandbox=SimpleNamespace(backend='smolvm', image=image)))
            async with build_graph_runtime(settings, []):
                assert not any(m['name'] == name for m in await listing(backend))
            assert not list(backend.ownership.directory.glob('*.lease'))
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
            await backend.sweep_orphans()
    asyncio.run(scenario())


def test_recovery_prunes_unregistered_dead_leases_but_keeps_live_ones(smolvm_cli, tmp_path):
    executable, _ = smolvm_cli
    backend = SmolvmSandbox('fixture', executable=str(executable), state_directory=tmp_path / 'state')
    backend.ownership.directory.mkdir(parents=True)
    dead = backend.ownership.directory / (uuid.uuid4().hex + '.lease')
    dead.touch(mode=0o600)
    async def scenario():
        with backend.ownership.create() as owner:
            live = backend.ownership.directory / (owner[LEASE] + '.lease')
            assert await backend.sweep_orphans() == ()
            assert not dead.exists()
            assert live.exists()
    asyncio.run(scenario())


def test_shutdown_waits_for_recovery_and_refuses_a_concurrent_start():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Backend:
            async def sweep_orphans(self):
                entered.set()
                await release.wait()
                return ()
        runtime = LangGraphRuntime(sandbox=Backend())
        start = asyncio.create_task(runtime.start(GraphId('unused'), {}))
        await entered.wait()
        close = asyncio.create_task(runtime.aclose())
        await asyncio.sleep(0)
        assert not close.done()
        release.set()
        await close
        with pytest.raises(RuntimeError, match='closing'):
            await start
        assert not runtime.running()
    asyncio.run(scenario())

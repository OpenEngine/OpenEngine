"""Durable ACP sessions across disposable VMs, approvals and steering."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import tarfile

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry, CodexACPProvider, ClaudeACPProvider

from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.domain import ApprovalDecision, ApprovalId
from engine.graph_runtime import EventLog, GraphId
from engine.graph_runtime_langgraph import ACPNode, LangGraphDefinition, LangGraphRuntime, SqliteGraphRuntimeStore, answer_permission
from engine.graph_runtime_langgraph.sandbox_sessions import AgentSessions
async def until(log, run_id, kind, *, cursor=0):
    async with asyncio.timeout(30):
        async for event in log.stream(run_id, cursor):
            assert event.kind.value != 'run.failed', event.payload
            if event.kind.value == kind:
                return list(log.since(run_id))
    raise AssertionError('unreachable')


@pytest.fixture(params=['fake', pytest.param('real', marks=pytest.mark.integration)])
def backend(request, tmp_path):
    if request.param == 'fake':
        executable, _ = request.getfixturevalue('smolvm_cli')
        return SmolvmSandbox('fixture', executable=str(executable), state_directory=tmp_path / 'state')
    image = os.environ.get('OE_SMOLVM_TEST_IMAGE')
    if not image:
        pytest.skip('set OE_SMOLVM_TEST_IMAGE for real VM session tests')
    return SmolvmSandbox(image, executable=os.environ.get('OE_SMOLVM_EXECUTABLE', 'smolvm'), state_directory=tmp_path / 'state')


def peer(source, provider):
    text = Path(__file__).with_name('acp_stub_agent.py').read_text()
    relative = '.codex/sessions' if provider == 'codex' else '.claude/projects'
    text = text.replace('return Path(os.environ["STUB_ACP_STATE"])', f'''directory = Path.home() / {relative!r}
    directory.mkdir(parents=True, exist_ok=True)
    return directory''')
    # A missing checkpoint must fail, rather than silently creating a session.
    text = text.replace('session = load(session_id)\n            session["loads"]', 'assert session_file(session_id).is_file(), "session store was lost"\n            session = load(session_id)\n            session["loads"]')
    (source / 'agent.py').write_text(text)


@asynccontextmanager
async def runtime_over(tmp_path, backend, provider, *, asks=False, human_review=False):
    source = tmp_path / 'source'
    source.mkdir(exist_ok=True)
    peer(source, provider)
    cls = CodexACPProvider if provider == 'codex' else ClaudeACPProvider
    agents = ACPAgentRegistry([cls(name='agent', command=('python3', 'agent.py'),
        permissions=answer_permission, env={'STUB_ACP_ASK': '1'} if asks else {})])
    store = SqliteGraphRuntimeStore(tmp_path / 'runs.db')
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / 'checkpoints.db')) as saver:
        builder = StateGraph(dict)
        builder.add_node('agent', ACPNode(agent='agent', prompt='Original task', cwd=str(source),
            registry=agents, graph_node_always_open=True))
        builder.add_edge(START, 'agent')
        if human_review:
            from engine.graph_runtime_langgraph import HumanReviewNode
            builder.add_node('human', HumanReviewNode())
            builder.add_edge('agent', 'human')
            builder.add_edge('human', END)
        else:
            builder.add_edge('agent', END)
        runtime = LangGraphRuntime(LangGraphDefinition(graph_id=GraphId('sessions'), name='Sessions',
            graph=builder.compile(checkpointer=saver)), store=store, sandbox=backend)
        log = EventLog()
        runtime.observe(log.append)
        try:
            yield runtime, log
        finally:
            await runtime.aclose()
            store.close()


def saved(store):
    result = []
    for archive in store.agent_sessions.directory.rglob('state.tar'):
        assert archive.stat().st_mode & 0o777 == 0o600
        with tarfile.open(archive) as incoming:
            for member in incoming.getmembers():
                if member.isfile() and member.name.endswith('.json'):
                    result.append(json.load(incoming.extractfile(member)))
    return result


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_permission_approval_resume_survives_vm_and_runtime_teardown(tmp_path, backend, provider):
    # Completed-run steering is covered by the human-review scenario below;
    # this case exercises the distinct pending ACP permission recovery path.
    async def exercise():
        async with runtime_over(tmp_path, backend, provider, asks=True) as (runtime, log):
            run = await runtime.start(GraphId('sessions'), {})
            events = await until(log, run.run_id, 'approval.requested')
            original = await runtime.store.session(run.run_id, 'agent')
            approval = ApprovalId(events[-1].payload['approvalId'])
        # Rebuild all runtime objects; only private files and SQLite survive.
        async with runtime_over(tmp_path, backend, provider, asks=True) as (runtime, log):
            assert saved(runtime.store)
            await runtime.decide(run.run_id, approval, ApprovalDecision.ACCEPT)
            events = await until(log, run.run_id, 'run.finished')
            started = next(event for event in events if event.kind.value == 'conversation.started')
            assert started.payload['resumed'] is True
            assert started.payload['sessionId'] == original.session_id
            state = saved(runtime.store)
            assert len(state) == 1
            assert state[0]['loads'] == 1
            assert len(state[0]['turns']) == 2
            assert state[0]['turns'][0] == 'Original task'
            assert state[0]['granted'] is True
            await runtime.store.delete_run(run.run_id)
            assert not saved(runtime.store)
            assert await runtime.store.session(run.run_id, 'agent') is None
    asyncio.run(exercise())


def test_session_directories_are_scoped_and_locked(tmp_path):
    async def exercise():
        sessions = AgentSessions(tmp_path / 'sessions')
        async with sessions.hold('../run', 'one', 'codex') as first:
            async with sessions.hold('../run', 'two', 'codex') as node:
                async with sessions.hold('other', 'one', 'claude') as other:
                    assert len({first.directory, node.directory, other.directory}) == 3
                    assert first.directory.is_relative_to(sessions.directory)
                    acquired = asyncio.Event()
                    async def wait():
                        async with sessions.hold('../run', 'one', 'codex'):
                            acquired.set()
                    task = asyncio.create_task(wait())
                    await asyncio.sleep(0)
                    assert not acquired.is_set()
        await task
        sessions.delete('../run')
        assert not first.directory.exists()
        assert other.directory.exists()
    asyncio.run(exercise())


@pytest.mark.parametrize('provider', ['codex', 'claude'])
@pytest.mark.parametrize('attack', ['credential', 'symlink'])
def test_only_selected_sessions_are_exported_and_bad_exports_preserve_checkpoint(tmp_path, backend, provider, attack):
    from engine.ports import SandboxSpec
    from engine.ports.sandbox_home import SandboxHome
    from engine.ports.sandbox_sessions import SandboxSessions
    async def exercise():
        storage = (tmp_path / 'private').resolve()
        storage.mkdir(mode=0o700)
        profile = SandboxSessions(provider, storage)
        relative = '.codex/sessions' if provider == 'codex' else '.claude/projects'
        home = SandboxHome(files={'.codex/auth.json': 'selected-secret', '.claude/model-token': 'selected-secret',
            '.git-credentials': 'forge-must-stay-in-guest'}, redactions=('selected-secret',))
        spec = SandboxSpec(home=home, sessions=profile, egress_allowlist=())
        async def write(vm, code):
            process = await vm.exec(('python3', '-c', code))
            _, stderr = await asyncio.gather(process.stdout.read(), process.stderr.read())
            assert await process.wait() == 0, stderr
        async with backend.create(spec) as vm:
            await write(vm, f"from pathlib import Path; p=Path.home()/{relative!r}; p.mkdir(parents=True); (p/'session.json').write_text('safe history')")
        original = (storage / 'state.tar').read_bytes()
        with tarfile.open(storage / 'state.tar') as archive:
            assert all('auth.json' not in name and 'model-token' not in name and 'git-credentials' not in name for name in archive.getnames())
        # Every rejection case first checkpoints and restores valid state.
        # Successful round trips also run in the approval/steering scenarios.
        with pytest.raises(ValueError) as caught:
            async with backend.create(spec) as vm:
                await write(vm, f"from pathlib import Path; p=Path.home()/{relative!r}; assert (p/'session.json').read_text() == 'safe history'")
                if attack == 'credential':
                    await write(vm, f"from pathlib import Path; (Path.home()/{relative!r}/'leak.json').write_text('selected-secret')")
                else:
                    await write(vm, f"from pathlib import Path; (Path.home()/{relative!r}/'leak.json').symlink_to(Path.home()/'.codex/auth.json')")
        assert 'selected-secret' not in str(caught.value)
        assert (storage / 'state.tar').read_bytes() == original
        from test_sandbox_lifecycle import listing
        assert not await listing(backend)
    asyncio.run(exercise())


@pytest.mark.parametrize('provider', ['codex', 'claude'])
def test_human_review_can_resume_and_then_steer_original_agent(tmp_path, backend, provider):
    async def exercise():
        async with runtime_over(tmp_path, backend, provider, human_review=True) as (runtime, log):
            run = await runtime.start(GraphId('sessions'), {})
            events = await until(log, run.run_id, 'approval.requested')
            approval = ApprovalId(events[-1].payload['approvalId'])
            original = await runtime.store.session(run.run_id, 'agent')
            assert saved(runtime.store)
        async with runtime_over(tmp_path, backend, provider, human_review=True) as (runtime, log):
            await runtime.decide(run.run_id, approval, ApprovalDecision.ACCEPT)
            await until(log, run.run_id, 'run.finished')
            await runtime.steer(run.run_id, 'Adjust after human review', node_id='agent')
            async with asyncio.timeout(30):
                async for event in log.stream(run.run_id):
                    assert event.kind.value != 'run.failed', event.payload
                    if event.kind.value == 'approval.requested':
                        approval = ApprovalId(event.payload['approvalId'])
                        if approval != events[-1].payload['approvalId']:
                            break
            await runtime.decide(run.run_id, approval, ApprovalDecision.ACCEPT)
            await until(log, run.run_id, 'run.finished', cursor=event.sequence)
            state = saved(runtime.store)
            assert state[0]['loads'] == 1
            assert state[0]['turns'] == ['Original task', 'Adjust after human review']
            binding = await runtime.store.session(run.run_id, 'agent')
            assert binding.session_id == original.session_id
    asyncio.run(exercise())


def test_missing_checkpoint_fails_without_starting_a_replacement_conversation(tmp_path, smolvm_cli):
    executable, _ = smolvm_cli
    backend = SmolvmSandbox('fixture', executable=str(executable), state_directory=tmp_path / 'state')
    async def exercise():
        async with runtime_over(tmp_path, backend, 'codex') as (runtime, log):
            run = await runtime.start(GraphId('sessions'), {})
            await until(log, run.run_id, 'run.finished')
        async with runtime_over(tmp_path, backend, 'codex') as (runtime, log):
            for archive in runtime.store.agent_sessions.directory.rglob('state.tar'):
                archive.unlink()
            await runtime.steer(run.run_id, 'Follow up', node_id='agent')
            async with asyncio.timeout(30):
                async for event in log.stream(run.run_id):
                    assert event.kind.value != 'conversation.started'
                    if event.kind.value == 'run.failed':
                        break
            assert (await runtime.snapshot(run.run_id)).status.value == 'failed'
    asyncio.run(exercise())

"""Guest MCP calls, host approvals, real host Git push, and a recording forge API."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_acp import ACPAgentRegistry, StdioACPProvider

from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.adapters.source_control.github import GitHubSourceControl
from engine.domain import AgentRunId, ApprovalDecision, RunId
from engine.graph_runtime import EventLog, GraphId
from engine.graph_runtime_langgraph import ACPNode, LangGraphDefinition, LangGraphRuntime, TerminalMcpServer
from engine.runtime.terminal_mcp import TerminalMcpBroker, TerminalResultRegistry

FORGE_TOKEN = 'host-forge-credential-678'


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(params=['fake', pytest.param('real', marks=pytest.mark.integration)])
def backend(request):
    if request.param == 'fake':
        executable, _ = request.getfixturevalue('smolvm_cli')
        return SmolvmSandbox('fixture', executable=str(executable))
    image = os.environ.get('OE_SMOLVM_TEST_IMAGE')
    if not image:
        pytest.skip('set OE_SMOLVM_TEST_IMAGE for real VM -> host broker acceptance')
    return SmolvmSandbox(image, executable=os.environ.get('OE_SMOLVM_EXECUTABLE', 'smolvm'))


@pytest.mark.parametrize('mode', ['publish', 'read-only', 'decline', 'leak', 'conflict'])
def test_guest_tools_use_host_git_and_forge_with_existing_approvals(backend, git_repo, tmp_path, monkeypatch, mode):
    source = git_repo(branch='agent/bridge-test', commit=True)
    remote = git_repo(bare=True)
    git(source, 'config', 'user.name', 'Host Test')
    git(source, 'config', 'user.email', 'host@example.test')
    git(source, 'remote', 'add', 'origin', 'https://github.com/acme/api.git')
    git(source, 'config', 'remote.origin.pushurl', str(remote))
    shutil.copy2(Path(__file__).with_name('sandbox_terminal_agent.py'), source / 'agent.py')
    monkeypatch.setenv('GITHUB_TOKEN', FORGE_TOKEN)
    bridge_tokens = []
    original = TerminalMcpBroker.sandbox_bridge
    async def capture(broker):
        bridge = await original(broker)
        bridge_tokens.append(bridge.token)
        return bridge
    monkeypatch.setattr(TerminalMcpBroker, 'sandbox_bridge', capture)
    calls, approvals, failures = [], [], []
    class Workspace:
        async def root_path(self, workspace_id):
            assert workspace_id == 'ws'
            return str(source)
    class Transport:
        host = 'github.com'
        async def request(self, method, path, **kwargs):
            # A real HTTP request on the host, never a VM network grant.
            async with httpx.AsyncClient() as client:
                response = await client.request(method, endpoint + path, headers={'Authorization': 'Bearer ' + FORGE_TOKEN}, **kwargs)
                response.raise_for_status()
                return response.json()
    class HostSource(GitHubSourceControl):
        async def authenticated_login(self, url):
            return 'host-user'
        async def can_write_repository(self, url, login):
            return True
    async def scenario():
        nonlocal endpoint
        async def forge(reader, writer):
            header = await reader.readuntil(b'\r\n\r\n')
            length = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            body = json.loads(await reader.readexactly(length))
            calls.append((header.decode(), body))
            payload = json.dumps({'html_url': 'https://github.com/acme/api/pull/7'}).encode()
            writer.write(b'HTTP/1.1 201 Created\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\nConnection: close\r\n\r\n' + payload)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(forge, '127.0.0.1', 0)
        endpoint = 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])
        sc = HostSource('', workspace_provider=Workspace(), transport=Transport())
        builder = StateGraph(dict)
        builder.add_node('agent', ACPNode(agent='test', registry=ACPAgentRegistry([
            StdioACPProvider(name='test', command=('python3', 'agent.py', mode)),
        ]), cwd=str(source), prompt='Implement and publish', workspace_policy='read-only' if mode == 'read-only' else 'apply',
            mcp_server_bindings=(TerminalMcpServer(step_id='agent', agent_id='test'),)))
        builder.add_edge(START, 'agent')
        builder.add_edge('agent', END)
        runtime = LangGraphRuntime(LangGraphDefinition(graph_id=GraphId('bridge'), name='Bridge',
            graph=builder.compile(checkpointer=InMemorySaver())), sandbox=backend, source_control=sc)
        events = EventLog()
        runtime.observe(events.append)
        try:
            run = await runtime.start(GraphId('bridge'), {'workspaceId': 'ws', 'repository': 'acme/api'})
            async with asyncio.timeout(180):
                async for event in events.stream(run.run_id):
                    if event.kind.value == 'approval.requested':
                        pending = (await runtime.snapshot(run.run_id)).pending_approvals
                        for approval in pending:
                            approvals.append(approval)
                            if mode == 'conflict':
                                (source / 'README.md').write_text('concurrent host edit\n')
                            await runtime.decide(run.run_id, approval.approval_id,
                                ApprovalDecision.CANCEL if mode == 'decline' else ApprovalDecision.ACCEPT)
                    if event.kind.value in {'run.finished', 'run.failed'}:
                        expected = 'run.failed' if mode in {'leak', 'conflict'} else 'run.finished'
                        assert event.kind.value == expected, event.payload
                        if mode == 'conflict':
                            assert 'patch' in event.payload
                        if mode == 'leak':
                            assert 'patch' not in event.payload
                            assert 'credentials' in str(event.payload)
                        failures.append(event.payload)
                        break
        finally:
            await runtime.aclose()
            server.close()
            await server.wait_closed()
    endpoint = ''
    asyncio.run(scenario())
    assert approvals, 'the guest must not bypass host approval'
    assert 'hostile' not in (source / '.git/config').read_text()
    if mode == 'publish':
        assert git(remote, 'show', 'refs/heads/agent/bridge-test:README.md') == 'implemented inside VM'
        assert git(source, 'log', '-1', '--format=%s') == 'feat: implement inside VM'
        assert len(calls) == 1
        assert 'POST /repos/acme/api/pulls ' in calls[0][0]
        assert 'Bearer ' + FORGE_TOKEN in calls[0][0]
        assert calls[0][1]['head'] == 'agent/bridge-test'
    else:
        assert not calls
        assert not git(remote, 'for-each-ref', 'refs/heads')
        if mode in {'read-only', 'leak'}:
            assert (source / 'README.md').read_text() == 'engine\n'
        elif mode == 'conflict':
            assert (source / 'README.md').read_text() == 'concurrent host edit\n'
        else:
            assert git(source, 'status', '--porcelain', '--', 'README.md'), 'declined Git call must not stage the final copied-out edit'
    assert not any(token in json.dumps(failures) for token in bridge_tokens)
    for path in source.rglob('*'):
        if path.is_file() and '.git' not in path.parts:
            assert FORGE_TOKEN.encode() not in path.read_bytes()
            assert not any(token.encode() in path.read_bytes() for token in bridge_tokens)


def test_bridge_token_scope_and_lifetime(tmp_path):
    async def scenario():
        async with TerminalMcpBroker(run_id=RunId('first'), agent_run_id=AgentRunId('one'), step=None, registry=TerminalResultRegistry()) as first, TerminalMcpBroker(run_id=RunId('second'), agent_run_id=AgentRunId('two'), step=None, registry=TerminalResultRegistry()) as second:
            bridge, other = await first.sandbox_bridge(), await second.sandbox_bridge()
            assert bridge.socket.host.stat().st_mode & 0o777 == 0o600
            assert bridge.socket.host.parent.stat().st_mode & 0o777 == 0o700
            async def request(token):
                reader, writer = await asyncio.open_unix_connection(bridge.socket.host)
                writer.write(json.dumps({'token': token, 'request': {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}}).encode() + b'\n')
                await writer.drain()
                result = json.loads(await reader.readline())
                writer.close()
                await writer.wait_closed()
                return result
            assert 'result' in await request(bridge.token)
            for token in ('wrong', other.token):
                assert (await request(token))['error']['message'] == 'terminal session is not authorized'
            path = bridge.socket.host
        assert not path.exists() and not path.parent.exists()
        with pytest.raises(OSError):
            await asyncio.open_unix_connection(path)
    asyncio.run(scenario())


def test_parallel_yaml_facets_cannot_mutate_the_host_workspace():
    from engine.graph_service.compile import compile_graph
    from engine.graph_service.language import parse_graph
    spec = parse_graph({'apiVersion': 'openengine.cc/v1', 'name': 'review', 'review': {
        'facets': {'agent': 'codex', 'prompt': 'Review', 'facets': [{'facet': 'security'}, {'facet': 'bugs'}]},
    }})
    workflow = compile_graph(spec, version_id='review-v1', number=1, workspace_provider=object(), registry=ACPAgentRegistry([]))
    for key in spec.nodes[0].keys:
        assert workflow.builder.nodes[key].runnable.afunc.workspace_policy == 'read-only'


def test_cancellation_revokes_the_broker_socket():
    async def scenario():
        ready = asyncio.Event()
        paths = []
        async def serve():
            async with TerminalMcpBroker(run_id=RunId('cancelled'), agent_run_id=AgentRunId('one'), step=None, registry=TerminalResultRegistry()) as broker:
                bridge = await broker.sandbox_bridge()
                paths.append(bridge.socket.host)
                ready.set()
                await asyncio.Future()
        task = asyncio.create_task(serve())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert paths and not paths[0].parent.exists()
    asyncio.run(scenario())


def test_correctable_host_tool_error_does_not_poison_workspace(git_repo):
    from engine.adapters.sandbox.process import ProcessSandbox
    from engine.graph_runtime_langgraph.sandbox_workspace import WorkspaceCopies
    from engine.ports import SandboxSpec
    source = git_repo(commit=True)
    async def scenario():
        async with WorkspaceCopies().create(str(source), 'apply') as copy:
            async with copy.backend(ProcessSandbox()).create(SandboxSpec()):
                with pytest.raises(RuntimeError, match='transient'):
                    async with copy.host_operation():
                        message = 'transient host API error'
                        raise RuntimeError(message)
                assert copy.failure is None
                async with copy.host_operation():
                    pass
            assert not await copy.accept()
    asyncio.run(scenario())


def test_branch_change_blocks_host_tools_even_without_guest_edits(git_repo):
    from engine.adapters.sandbox.process import ProcessSandbox
    from engine.graph_runtime_langgraph.sandbox_workspace import WorkspaceCopies, WorkspacePatchConflict
    from engine.ports import SandboxSpec
    source = git_repo(commit=True)
    async def scenario():
        async with WorkspaceCopies().create(str(source), 'apply') as copy:
            async with copy.backend(ProcessSandbox()).create(SandboxSpec()):
                git(source, 'checkout', '-b', 'other-branch')
                with pytest.raises(WorkspacePatchConflict):
                    async with copy.host_operation():
                        pytest.fail('host operation ran against a different branch')
    asyncio.run(scenario())


@pytest.mark.parametrize('arguments', [
    ['config', '--list'], ['add', '.'], ['diff', '--output=/tmp/unsafe'], ['diff', '--ext-diff'],
    ['show', '--textconv'], ['log', '--show-signature'], ['branch', 'new'], ['branch', '--list', '--delete', 'main'],
])
def test_read_only_inspection_cannot_launch_programs_or_write_files(arguments):
    from engine.graph_runtime_langgraph.sandbox_workspace import _inspection_git
    with pytest.raises(ValueError):
        _inspection_git(arguments)

"""Run the real default graph; host ACP launches are forbidden in this test."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest
from langgraph_acp import ACPAgentRegistry, CodexACPProvider, ClaudeACPProvider, StdioACPProvider

from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.adapters.workspace_provider.git_worktree import GitWorktreeWorkspaceProvider
from engine.apps.web import composition
from engine.apps.web.composition import Settings, build_graph_runtime
from engine.domain import ApprovalDecision
from engine.graph_runtime import EventLog, GraphId
from engine.runtime.config import parse_engine_config
from test_graph_workflow_definitions import definition_module


@pytest.mark.parametrize('hardware', ['fake', pytest.param('real', marks=pytest.mark.integration)])
def test_default_workflow_never_launches_an_agent_on_host(tmp_path, git_repo, monkeypatch, request, hardware):
    image = os.environ.get('OE_SMOLVM_TEST_IMAGE')
    if hardware == 'real' and not image:
        pytest.skip('set OE_SMOLVM_TEST_IMAGE for the real default-workflow check')
    executable = os.environ.get('OE_SMOLVM_EXECUTABLE', 'smolvm')
    if hardware == 'fake':
        executable, _ = request.getfixturevalue('smolvm_cli')
        image = 'fixture'
    source = git_repo(commit=True)
    shutil.copy2(Path(__file__).with_name('default_workflow_sandbox_agent.py'), source / 'agent.py')
    for arguments in (['add', 'agent.py'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-m', 'test: add guest peer']):
        subprocess.run(['git', '-C', str(source), *arguments], check=True, capture_output=True)
    launches = []
    class RecordingSandbox(SmolvmSandbox):
        @asynccontextmanager
        async def create(self, spec):
            node = spec.labels['node']
            async with super().create(replace(spec, secrets={**spec.secrets, 'OE_TEST_NODE': node})) as vm:
                launches.append((node, vm.name))
                yield vm
    monkeypatch.setattr(composition, 'SmolvmSandbox', lambda **kwargs: RecordingSandbox(executable=str(executable), **kwargs))
    monkeypatch.setattr(composition, 'host_model_credential', lambda *_: None)
    async def forbidden(*args, **kwargs):
        pytest.fail('a graph agent was launched on the host')
    for cls in (CodexACPProvider, ClaudeACPProvider, StdioACPProvider):
        monkeypatch.setattr(cls, 'connect', forbidden)
    agents = ACPAgentRegistry([
        CodexACPProvider(command=('python3', 'agent.py')),
        ClaudeACPProvider(command=('python3', 'agent.py')),
    ])
    workspace = GitWorktreeWorkspaceProvider(str(tmp_path / 'worktrees'))
    module = definition_module()
    workflow = module.graph_for("codex", workspace_provider=workspace, agents=agents)
    # No backend setting: exercise the production default through composition.
    settings = replace(Settings(), graph_state_directory=str(tmp_path / 'graph-state'),
        engine_config=parse_engine_config({'sandbox': {'image': image}}))
    async def exercise():
        async with build_graph_runtime(settings, [workflow], source_control=object()) as runtime:
            log = EventLog()
            runtime.observe(log.append)
            run = await runtime.start(GraphId('implementation-review-rerank'), {
                'repository': str(source), 'task': 'Implement a small local change',
                'inputs': {'mode': 'disconnected', 'spec': 'true', 'adversarial': 'true'},
            })
            async with asyncio.timeout(240):
                async for event in log.stream(run.run_id):
                    assert event.kind.value != 'run.failed', event.payload
                    if event.kind.value == 'approval.requested':
                        assert event.node_id == 'human-review'
                        await runtime.decide(run.run_id, event.payload['approvalId'], ApprovalDecision.ACCEPT)
                    if event.kind.value == 'run.finished':
                        break
            snapshot = await runtime.snapshot(run.run_id)
            assert snapshot.status.value == 'completed'
            assert (Path(snapshot.values['workspace']) / 'README.md').read_text() == 'Implemented inside the VM\n'
            proofs = []
            for path in runtime.store.agent_sessions.directory.rglob('state.tar'):
                with tarfile.open(path) as archive:
                    for member in archive.getmembers():
                        if member.name.endswith('/proof.json'):
                            proofs.append(json.load(archive.extractfile(member)))
            expected = {'spec', 'naming', 'implementation', 'refute', 'reranker', 'impact-analysis',
                *(f'review-{facet.id}' for facet in module.REVIEW_FACETS)}
            assert {proof['vmProof'] for proof in proofs} == expected
            assert {node for node, _ in launches} == expected
            assert len({name for _, name in launches}) == len(expected)
            for proof in proofs:
                assert proof['cwd'] != str(source)
                assert proof['home'] != str(Path.home())
                if hardware == 'real':
                    assert proof['kernel'] == 'Linux'
                    assert proof['cwd'] == '/workspace'
    asyncio.run(exercise())

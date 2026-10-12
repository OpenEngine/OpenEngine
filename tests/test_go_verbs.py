"""Project contracts, target resolution and independent verb graph behavior."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.runnables import RunnableLambda

from engine.cli import starters
from engine.graph_service import targets
from engine.graph_service.service import ServiceError
from engine.graph_runtime_langgraph.components import change_graphs as graphs
from engine.runtime.workflows import load_workflow_file
from test_graph_service import PAIR, graph_service


@pytest.mark.parametrize('target,repo,number,branch', [
    ('https://github.com/Owner/Repo/pull/42', '', 42, ''),
    ('#42', 'Owner/Repo', 42, ''),
    ('Owner/Repo#42', '', 42, ''),
    ('topic', 'Owner/Repo', 0, 'topic'),
])
def test_target_forms(monkeypatch, target, repo, number, branch):
    inspect = AsyncMock(return_value={'exists': True, 'branch': 'topic', 'pr_url': 'https://github.com/Owner/Repo/pull/42'})
    service = SimpleNamespace(resolve_repository=lambda value: value,
                              runtime=SimpleNamespace(source_control=SimpleNamespace(inspect_target=inspect)))
    monkeypatch.setattr(targets, 'resolve_forge_context', AsyncMock(return_value={'mode': 'connected'}))
    result = asyncio.run(targets.resolve_target(service, repo, target))
    assert result == {'repo': 'Owner/Repo', 'ref': 'origin/topic', 'pr_url': 'https://github.com/Owner/Repo/pull/42', 'mode': 'connected'}
    inspect.assert_awaited_once_with('Owner/Repo', number=number, branch=branch, include_threads=False)


def test_target_infers_current_branch_and_open_pr(monkeypatch, tmp_path):
    async def git(path, *args):
        return {'remote': 'git@github.com:o/r.git', 'symbolic-ref': 'topic', 'show-ref': 'abc'}[args[0]]
    inspect = AsyncMock(return_value={'exists': True, 'branch': 'topic', 'pr_url': 'https://github.com/o/r/pull/8'})
    service = SimpleNamespace(resolve_repository=lambda value: value,
                              runtime=SimpleNamespace(source_control=SimpleNamespace(inspect_target=inspect)))
    monkeypatch.setattr(targets, '_git', git)
    monkeypatch.setattr(targets, 'resolve_forge_context', AsyncMock(return_value={'mode': 'disconnected'}))
    result = asyncio.run(targets.resolve_target(service, str(tmp_path), '', local=True))
    assert result['ref'] == 'topic' and result['pr_url'].endswith('/8')
    inspect.assert_awaited_once_with('o/r', number=0, branch='topic', include_threads=False)


@pytest.mark.parametrize('details,mode,error', [
    ({'private': True}, 'disconnected', 'run engine connect github'),
    ({'exists': False}, 'connected', 'no such branch or PR; to give an instruction, quote it'),
])
def test_target_refusals(monkeypatch, details, mode, error):
    service = SimpleNamespace(resolve_repository=lambda value: value,
        runtime=SimpleNamespace(source_control=SimpleNamespace(inspect_target=AsyncMock(return_value=details))))
    monkeypatch.setattr(targets, 'resolve_forge_context', AsyncMock(return_value={'mode': mode}))
    with pytest.raises(ServiceError, match=error):
        asyncio.run(targets.resolve_target(service, 'o/r', 'missing'))


def test_project_bindings_are_validated_persisted_and_exposed(tmp_path):
    import httpx
    from engine.graph_service.api import create_app

    async def scenario():
        async with graph_service(tmp_path) as service:
            assert service.verbs('one') == starters.VERBS
            await service.add_graph('one', source=PAIR.replace('inputs:\n', 'inputs:\n  instruction: {description: Task}\n'))
            assert service.bind_verb('one', 'go', 'pair')['go'] == 'pair'
            with pytest.raises(ServiceError, match='must declare pr_url or ref'):
                service.bind_verb('one', 'review', 'pair')
            await service.add_graph('one', source=starters.source('review'), format='python')
            assert service.bind_verb('one', 'audit', starters.DEFAULT)['audit'] == starters.DEFAULT
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(service)), base_url='http://test') as client:
                assert (await client.get('/verbs', params={'project': 'one'})).json()['verbs']['go'] == 'pair'
                refused = await client.post('/verbs/review', json={'project': 'one', 'graph': 'pair'})
                assert refused.status_code == 400
                registered = (await client.get(f'/graphs/{starters.DEFAULT}', params={'project': 'one'})).json()
                assert {'spec', 'review_only', 'adversarial', 'fix_only'} <= set(registered['inputs'])

        async with graph_service(tmp_path) as service:
            assert service.verbs('one')['go'] == 'pair'
            assert service.verbs('two') == starters.VERBS
    asyncio.run(scenario())


@pytest.mark.parametrize('entry', ['implement', 'review', 'fix'])
@pytest.mark.parametrize('mode', ['connected', 'disconnected'])
def test_builtin_default_routes_inputs(tmp_path, entry, mode):
    name = starters.DEFAULT
    path = tmp_path / f'{name}.py'
    path.write_text(starters.source(name))
    workflow = load_workflow_file(path)[0]
    assert str(workflow.graph_id) == name
    inputs = {item.name for item in workflow.inputs}
    assert {'spec', 'review_only', 'adversarial', 'fix_only'} <= inputs
    visited = []

    def stub(key):
        def invoke(state):
            visited.append(key)
            if key in ('ci-check', 'fix-ci'):
                return {'ci_check': {'passed': True}}
            if key == 'reranker':
                return {'review_rounds': state.get('review_rounds', 0) + 1, 'review': [{'tagline': 'Bug', 'description': 'Fix it'}]}
            if key == 'threads':
                return {'threads': [{'thread_id': 'T', 'body': 'Fix it'}]}
            return {}
        return RunnableLambda(invoke)
    for key, node in workflow.builder.nodes.items():
        node.runnable = stub(key)
    asyncio.run(workflow.builder.compile().ainvoke({'task': 'docs', 'inputs': {'mode': mode, 'spec': 'true', 'adversarial': 'true', 'review_only': str(entry == 'review').lower(), 'publish_review': 'true', 'fix_only': str(entry == 'fix').lower()}}))
    if entry == 'implement':
        assert visited.count('implementation') == 2 and visited.count('reranker') == 2
        assert visited.index('spec') < visited.index('implementation')
        assert 'threads' not in visited and visited.count('refute') == 2
        assert visited[-1] == 'human-review'
    elif entry == 'review':
        assert 'implementation' not in visited and 'ci-check' not in visited
        assert visited.index('refute') < visited.index('reranker') < visited.index('impact-analysis')
    else:
        assert visited == ['workspace', 'threads', 'fix-implementation', 'fix-ci', 'reply']


@pytest.mark.parametrize('mode', ['connected', 'disconnected'])
def test_shared_prompts_and_thread_loading_respect_mode(mode, monkeypatch):
    from engine.ports import ChangeRequest, Discussion
    change = ChangeRequest(1, 'Fix', 'open', '', 'dev', 'https://github.com/o/r/pull/1', 'topic', 'abc', 'main',
        comments=(Discussion('dev', 'needed', '', comment_id=4, thread_id='T', is_resolved=False),
                  Discussion('dev', 'done', '', comment_id=5, thread_id='R', is_resolved=True)))
    source = SimpleNamespace(view_change_request=AsyncMock(return_value=change))
    monkeypatch.setattr(graphs, 'current_execution', lambda: SimpleNamespace(runtime=SimpleNamespace(source_control=source)))
    state = {'workspaceId': 'ws-1', 'inputs': {'mode': mode, 'pr_url': change.url, 'review_threads': '[{"body":"cached"}]'}}
    result = asyncio.run(graphs.review_threads(state))
    if mode == 'connected':
        assert len(result['threads']) == 1 and result['threads'][0]['thread_id'] == 'T'
        assert result['inputs']['ref'] == 'topic'
    else:
        assert result['threads'] == [{'body': 'cached'}]
        source.view_change_request.assert_not_called()
    c = graphs.Components('codex', None, None)
    prompt = c.rank().prompt(state)
    assert ('post it as a PR comment' in prompt) == (mode == 'connected')
    prompt = c.implementation(fixing=True).prompt(state)
    assert ('commit and push' in prompt) == (mode == 'connected')


def test_runner_override_updates_tool_identity():
    c = graphs.Components('codex', None, None)
    for node in (c.implementation(), c.rank(), c.impact(), *c.reviews().values()):
        selected = node._for_runner('claude')
        assert selected.agent == 'claude'
        assert all(binding.agent_id == 'claude' for binding in selected.mcp_server_bindings)


def test_go_review_uses_callers_branch_on_remote_backend(monkeypatch, tmp_path):
    from engine.cli import backends, repository
    from engine.apps.cli.__main__ import main
    from test_graph_cli import serve_go, RUN
    monkeypatch.setenv(backends.FILE_ENVIRONMENT_VARIABLE, str(tmp_path / 'backends.json'))
    monkeypatch.setenv('ENGINE_CLI_CONFIG', str(tmp_path / 'cli.json'))
    monkeypatch.delenv('ENGINE_SERVICE_TOKEN', raising=False)
    backends.add('mini', 'http://mini:4364')
    monkeypatch.setattr(repository, 'current', lambda: repository.Repository(tmp_path, 'o/r', 'caller-topic', False))
    recorded = serve_go(monkeypatch, RUN, graph=starters.DEFAULT, target={'ref': 'origin/caller-topic'})
    assert main(['go', 'review', '--backend', 'mini']) == 0
    assert recorded.requests[1]['body']['target'] == 'caller-topic'


def test_review_pr_uses_pull_ref_even_when_branch_lives_in_fork(monkeypatch):
    details = {'exists': True, 'branch': 'fork-topic', 'pr_url': 'https://github.com/o/r/pull/9',
               'checkout_ref': 'origin/refs/pull/9/head'}
    service = SimpleNamespace(resolve_repository=lambda value: value,
        runtime=SimpleNamespace(source_control=SimpleNamespace(inspect_target=AsyncMock(return_value=details))))
    monkeypatch.setattr(targets, 'resolve_forge_context', AsyncMock(return_value={'mode': 'connected'}))
    result = asyncio.run(targets.resolve_target(service, '', 'https://github.com/o/r/pull/9'))
    assert result['ref'] == 'origin/refs/pull/9/head'


@pytest.mark.parametrize('name', starters.NAMES)
def test_builtin_disconnected_bindings_expose_no_forge_tools(tmp_path, monkeypatch, name):
    from engine.domain import RunId
    from engine.graph_runtime_langgraph.graphs import unwrapped
    from engine.runtime.terminal_mcp import TerminalMcpBroker
    path = tmp_path / f'{name}.py'
    path.write_text(starters.source(name))
    workflow = load_workflow_file(path)[0]
    served = []
    def enable(self, source, names, workspace, approve):
        served.append((tuple(names), self._step.required_outputs))
    monkeypatch.setattr(TerminalMcpBroker, 'enable_repository_tools', enable)
    execution = SimpleNamespace(run_id=RunId('run'), execution_id='one', node_id='node',
        runtime=SimpleNamespace(source_control=SimpleNamespace(), store=SimpleNamespace(), workorder_creator=None))
    async def scenario():
        for spec in workflow.builder.nodes.values():
            for binding in getattr(unwrapped(spec.runnable), 'mcp_server_bindings', ()):
                async with binding({'workspaceId': 'ws', 'inputs': {'mode': 'disconnected'}}, execution, None):
                    pass
    asyncio.run(scenario())
    assert served
    assert all(set(names) <= {'git_subcommand'} and 'pr_url' not in outputs for names, outputs in served)


@pytest.mark.parametrize('head_repository', ['fork/r', '', 'o/r'])
@pytest.mark.parametrize('mode', ['connected', 'disconnected'])
def test_fix_pr_rejects_fork_or_missing_head(monkeypatch, head_repository, mode):
    details = {'exists': True, 'branch': 'main', 'pr_url': 'https://github.com/o/r/pull/9',
               'head_repository': head_repository}
    service = SimpleNamespace(resolve_repository=lambda value: value,
        runtime=SimpleNamespace(source_control=SimpleNamespace(inspect_target=AsyncMock(return_value=details))))
    monkeypatch.setattr(targets, 'resolve_forge_context', AsyncMock(return_value={'mode': mode}))
    if head_repository == 'o/r':
        assert asyncio.run(targets.resolve_target(service, 'o/r', '#9', verb='fix'))['ref'].endswith('origin/main')
    else:
        with pytest.raises(ServiceError, match='cannot fix a fork PR'):
            asyncio.run(targets.resolve_target(service, 'o/r', '#9', verb='fix'))


def test_direct_fix_rejects_fork_before_provisioning(monkeypatch):
    provider = SimpleNamespace(provision_branch=AsyncMock())
    source = SimpleNamespace(inspect_target=AsyncMock(return_value={'head_repository': 'fork/r'}))
    monkeypatch.setattr(graphs, 'current_execution', lambda: SimpleNamespace(runtime=SimpleNamespace(source_control=source)))
    with pytest.raises(ValueError, match='cannot fix a fork PR'):
        asyncio.run(graphs.FixWorkspace(provider=provider)({'inputs': {'pr_url': 'https://github.com/o/r/pull/9', 'ref': 'origin/main'}}))
    provider.provision_branch.assert_not_called()

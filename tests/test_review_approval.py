"""Review publication and deterministic approval, including the executed path."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.runnables import RunnableLambda

from engine.graph_runtime.inputs import resolve_inputs
from engine.graph_runtime_langgraph.components import change_graphs as graphs
from engine.graph_runtime_langgraph.components import review_publish as publish
from engine.ports import PipelineStatus, StatusCheck, Pipeline


URL = 'https://github.com/owner/repo/pull/42'


@pytest.fixture
def execution(monkeypatch):
    source = SimpleNamespace(
        view_change_request=AsyncMock(return_value=SimpleNamespace(author='contributor')),
        authenticated_login=AsyncMock(return_value='engine'),
        list_pipeline_status=AsyncMock(return_value=PipelineStatus('sha', (), ())),
        add_comment=AsyncMock(), approve_change_request=AsyncMock(),
    )
    execution = SimpleNamespace(runtime=SimpleNamespace(source_control=source), say=AsyncMock())
    monkeypatch.setattr(publish, 'current_execution', lambda: execution)
    return execution


def state(**updates):
    return {'workspaceId': 'ws', 'impact_level': 'Green', 'impact_rationale': 'A small, covered change.',
            'review': [], 'inputs': {'mode': 'connected', 'pr_url': URL, 'publish': 'true',
                                     'auto_approve_green': 'true'}, **updates}


@pytest.mark.parametrize('changes,inputs,reason', [
    ({}, {'auto_approve_green': 'false'}, 'disabled'),
    ({}, {'mode': 'disconnected'}, 'disconnected'),
    ({}, {'publish': 'false'}, 'publishing is disabled'),
    ({}, {'pr_url': ''}, 'no PR URL'),
    ({'impact_level': 'Orange'}, {}, 'not Green'),
    ({'impact_level': 'Red'}, {}, 'not Green'),
    ({'review': [{'tagline': 'Bug', 'description': 'Wrong result.'}]}, {}, 'survived'),
    ({}, {}, ''),
])
def test_approval_conditions_and_executed_path(execution, changes, inputs, reason):
    workflow = graphs.review(graphs.Components('codex', None, None))
    visited = []
    for key, node in workflow.builder.nodes.items():
        if key in {'approval-check', 'approve', 'publish-review'}:
            original = node.runnable
            async def invoke(s, original=original, key=key):
                visited.append(key)
                return await original.ainvoke(s)
            node.runnable = RunnableLambda(invoke)
        else:
            node.runnable = RunnableLambda(lambda s: {})
    given = state(**changes)
    given['inputs'].update(inputs)
    result = asyncio.run(workflow.builder.compile().ainvoke(given))
    assert ('approve' in visited) is (not reason)
    assert ('publish-review' in visited) is publish.publishing(given)
    assert reason in result['approval_reason']
    assert execution.runtime.source_control.approve_change_request.await_count == (0 if reason else 1)
    assert any(('skipped' if reason else 'Approved automatically') in call.args[0]
               for call in execution.say.await_args_list)


@pytest.mark.parametrize('status,conclusion,eligible', [
    ('completed', 'success', True), ('completed', 'neutral', True),
    ('pending', None, True), ('in_progress', None, True),
    ('completed', 'failure', False), ('failed', None, False),
    ('completed', 'cancelled', False), ('completed', 'timed_out', False),
    ('completed', None, False), ('unknown', None, False),
])
@pytest.mark.parametrize('location', ['check', 'pipeline', 'required'])
def test_ci_blocks_approval(execution, status, conclusion, eligible, location):
    check = StatusCheck('test', status, conclusion, '')
    pipeline = Pipeline(1, 'test', status, conclusion, '')
    execution.runtime.source_control.list_pipeline_status.return_value = PipelineStatus(
        'sha', (check,) if location == 'check' else (),
        (pipeline,) if location == 'pipeline' else (),
        (check,) if location == 'required' else (),
    )
    reason = asyncio.run(publish.approval_reason(state()))
    assert (not reason) is eligible


def test_self_approval_is_visible_and_does_not_fail(execution):
    execution.runtime.source_control.view_change_request.return_value.author = 'ENGINE'
    result = asyncio.run(publish.ApproveReview()(state()))
    assert result['approved'] is False
    assert 'approving identity' in result['approval_reason']
    execution.runtime.source_control.approve_change_request.assert_not_awaited()
    assert 'skipped' in execution.say.await_args.args[0]


def test_unavailable_forge_skips_approval(execution):
    execution.runtime.source_control.list_pipeline_status.side_effect = RuntimeError('unavailable')
    assert 'could not verify' in asyncio.run(publish.approval_reason(state()))


def test_publish_posts_inline_findings_and_impact(execution):
    finding = {'tagline': 'Bug', 'description': 'Wrong result.', 'file': 'app.py', 'line': 7,
               'agent': 'codex', 'facet': 'bugs'}
    asyncio.run(publish.PublishReview()(state(review=[finding])))
    calls = execution.runtime.source_control.add_comment.await_args_list
    assert len(calls) == 2
    assert calls[0].kwargs == {'file': 'app.py', 'line': 7}
    assert 'agent: codex' in calls[0].args[1]
    assert calls[1].args == (URL, 'Impact: Green\n\nA small, covered change.')


def test_approval_body_has_rationale(execution):
    asyncio.run(publish.ApproveReview()(state()))
    args = execution.runtime.source_control.approve_change_request.await_args.args
    assert args[:2] == ('owner/repo', 42)
    assert 'Approved automatically because impact was Green' in args[2]
    assert state()['impact_rationale'] in args[2]


def test_review_topology_and_local_agent_tools():
    from engine.graph_runtime_langgraph.graphs import unwrapped
    workflow = graphs.review(graphs.Components('codex', None, None))
    graph = workflow.builder.compile().get_graph()
    edges = {(e.source, e.target, e.conditional) for e in graph.edges}
    assert ('approval-check', 'approve', True) in edges
    assert ('approval-check', '__end__', True) in edges
    assert ('impact', 'publish-review', True) in edges
    assert ('publish-review', 'approval-check', False) in edges
    for key in ('reranker', 'impact'):
        node = unwrapped(workflow.builder.nodes[key].runnable)
        assert 'add_comment' not in node.mcp_server_bindings[0].repository_tools
        assert 'do not post' in node.prompt(state()).lower()
    assert resolve_inputs(workflow.inputs, {})['publish'] == 'false'


@pytest.mark.parametrize('global_value,override,explicit,expected', [
    (False, None, None, 'false'), (True, None, None, 'true'),
    (False, True, None, 'true'), (True, False, None, 'false'),
    (False, False, 'true', 'true'), (True, True, 'false', 'false'),
])
def test_cli_run_input_precedence(tmp_path, global_value, override, explicit, expected):
    from engine.cli import starters
    from engine.runtime.config import PullRequestsConfig
    from test_graph_service import graph_service

    async def scenario():
        config = PullRequestsConfig(global_value, {} if override is None else {'example/repo': override})
        async with graph_service(tmp_path, pull_requests=config) as service:
            await service.add_graph('one', source=starters.source('review'), format='python')
            _, version = service.resolve('one', 'review')
            inputs = {} if explicit is None else {'auto_approve_green': explicit}
            request = service._start_request(version, 'Review', inputs, 'example/repo')
            assert request.inputs['auto_approve_green'] == expected
    asyncio.run(scenario())


def test_webhook_resolves_review_name_in_repository_project(tmp_path):
    from engine.apps.web.api import _GraphSurface
    from engine.cli import starters
    from test_graph_service import graph_service

    async def scenario():
        async with graph_service(tmp_path) as service:
            await service.add_graph('owner/repo', source=starters.source('review'), format='python')
            await service.add_graph('other/repo', source=starters.source('review'), format='python')
            surface = _GraphSurface(runtime=service.runtime, service=service)
            workflow = surface.named_workflow({}, 'owner/repo', 'review')
            _, version = service.resolve('owner/repo', 'review')
            assert str(workflow.graph_id) == version.version_id
            assert 'approve' in workflow.builder.nodes
    asyncio.run(scenario())

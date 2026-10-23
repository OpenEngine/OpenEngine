import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph_acp.events import ACPEventType
from engine.domain import RunOrigin
from engine.github_concierge.github_concierge import FeedbackRequest, GithubConcierge, classify_feedback

EXAMPLE = """1. Why did we move this?
2. This file is now just imports and a redirect. Why don't we just point the caller to the new location?
Just answer, don't make modifications."""


@pytest.mark.parametrize(('text', 'intent'), [
    (EXAMPLE, 'answer'), ("Why don't we just point the caller to the new location?", 'both'),
    ('Question: replace the redirect?', 'answer'), ('Please fix this. No changes.', 'answer'),
    ('Why was this moved?', 'answer'), ('Why did we move this?', 'answer'), ('Please replace the redirect.', 'change'),
    ('Why was this moved? Please remove it.', 'both'),
    ('@OpenEngineBot could you please fix the null handling?', 'both'),
    ('Can you please update this?', 'both'),
    ('Would you please remove this? Just answer.', 'answer'),
])
def test_classification(text, intent):
    assert classify_feedback(text) == intent


def test_answer_has_no_tools_or_workorder_and_is_replied_once():
    asyncio.run(_answer_once())


async def _answer_once():
    class Session:
        async def prompt(self, prompt):
            assert EXAMPLE in prompt
            assert 'diff evidence' in prompt
            yield SimpleNamespace(type=ACPEventType.MESSAGE_DELTA,
                                  data={'content': {'type': 'text', 'text': '1. Compatibility. 2. Direct imports would be better; I can change them.'}})
    client = SimpleNamespace(new_session=AsyncMock(return_value=Session()), close=AsyncMock())
    provider = SimpleNamespace(connect=AsyncMock(return_value=client))
    forward, reply = AsyncMock(), AsyncMock()
    concierge = GithubConcierge(provider=provider, answer_provider=provider, continue_workorder=forward, reply=reply,
                                 context=AsyncMock(return_value='diff evidence'))
    request = FeedbackRequest(RunOrigin(channel='github:acme/api', thread_id='7/review/11', author='reviewer'), EXAMPLE, comment_id='12')
    await concierge.handle(request)
    await concierge.handle(request)
    forward.assert_not_awaited()
    assert client.new_session.call_args.kwargs['mcp_servers'] == []
    assert client.new_session.call_args.kwargs['session_config']['claudeCode']['options'] == {
        'tools': [], 'allowedTools': [],
    }
    reply.assert_awaited_once()
    assert reply.call_args.args[0].thread_id == '7/review/11'
    await concierge.close()


def test_context_uses_only_forge_reads_and_decodes_surrounding_code():
    import base64
    import json
    from engine.adapters.source_control.github import GitHubSourceControl
    source = GitHubSourceControl('')
    async def api(method, path):
        assert method == 'GET'
        if '/contents/' in path:
            assert path.endswith('?ref=abc')
            return {'encoding': 'base64', 'content': base64.b64encode(b'def caller(): pass').decode()}
        if '/issues/' in path:
            return {'body': 'linked requirements'}
        if '/files?' in path:
            return [{'filename': 'caller.py', 'patch': 'diff evidence'}]
        if '/commits?' in path:
            return [{'commit': {'message': 'reason'}}]
        return {'body': 'Closes #42', 'head': {'sha': 'abc'}}
    source._api = AsyncMock(side_effect=api)
    context = json.loads(asyncio.run(source.review_context('https://github.com/acme/api/pull/7')))
    assert context['source']['caller.py'] == 'def caller(): pass'
    assert context['linked_issues'][0]['body'] == 'linked requirements'
    assert context['commits'][0]['commit']['message'] == 'reason'


def test_failed_reply_reuses_answer_on_redelivery():
    async def run():
        forward = AsyncMock()
        reply = AsyncMock(side_effect=[RuntimeError("offline"), None])
        concierge = GithubConcierge(provider=SimpleNamespace(), continue_workorder=forward,
                                     reply=reply, context=AsyncMock())
        # A generated answer is cached before the potentially failing network post.
        origin = RunOrigin(channel="github:acme/api", thread_id="7/review/11", author="reviewer")
        request = FeedbackRequest(origin, EXAMPLE, comment_id="12")
        concierge._answered[concierge._forwarded_key(request)] = "The redirect preserves compatibility."
        with pytest.raises(RuntimeError, match="offline"):
            await concierge.handle(request)
        await concierge.handle(request)
        await concierge.handle(request)
        assert reply.await_count == 2
        forward.assert_not_awaited()
        concierge.context.assert_not_awaited()
    asyncio.run(run())


def test_mixed_feedback_answers_and_forwards_once():
    from provider_fakes import FakeACPProvider
    from engine.github_concierge import Continuation
    async def run():
        forward = AsyncMock(return_value=Continuation(run_id='work', url=''))
        reply = AsyncMock()
        concierge = GithubConcierge(provider=FakeACPProvider(create=True),
                                     continue_workorder=forward, reply=reply)
        concierge._answer = AsyncMock(return_value='Direct imports would remove the redirect.')
        request = FeedbackRequest(
            RunOrigin(channel='github:acme/api', thread_id='7/review/11', author='reviewer'),
            "Why don't we just update the caller? new workorder", comment_id='12', allow_start=True,
        )
        try:
            await concierge.handle(request)
            await concierge.handle(request)
            forward.assert_awaited_once()
            assert reply.await_args_list[0].args[1] == 'Direct imports would remove the redirect.'
            assert sum(c.args[1] == 'Direct imports would remove the redirect.' for c in reply.await_args_list) == 1
        finally:
            await concierge.close()
    asyncio.run(run())


@pytest.mark.parametrize('large', [False, True])
def test_inline_context_is_bounded_and_survives_missing_issue(large):
    import base64
    import json
    from engine.adapters.source_control.github import GitHubSourceControl, GitHubSourceControlError

    source = GitHubSourceControl('')
    async def api(method, path):
        assert method == 'GET'
        if path.endswith('/pulls/comments/11'):
            return {'id': 11, 'path': 'target.py', 'line': 17, 'diff_hunk': '@@ target', 'body': 'Why this?'}
        if '/comments?' in path:
            return [{'id': 11, 'body': 'Why this?'}, {'id': 12, 'in_reply_to_id': 11, 'body': 'Earlier explanation'},
                    {'id': 13, 'in_reply_to_id': 99, 'body': 'Unrelated'}]
        if '/files?' in path:
            return [{'filename': 'other.py', 'patch': 'x' * (200_000 if large else 2)},
                    {'filename': 'target.py', 'patch': '@@ target'}] + [{'filename': f'{i}.py'} for i in range(98)]
        if '/commits?' in path:
            return []
        if '/issues/42' in path:
            raise GitHubSourceControlError('404')
        if '/issues/43' in path:
            return {'body': 'available requirements'}
        if '/contents/' in path:
            assert '/contents/target.py?' in path
            return {'encoding': 'base64', 'content': base64.b64encode(b'target source').decode()}
        return {'body': 'Closes #42 and #43', 'head': {'sha': 'abc'}}
    source._api = AsyncMock(side_effect=api)
    raw = asyncio.run(source.review_context('https://github.com/acme/api/pull/7', comment_id=11))
    context = json.loads(raw)
    assert len(raw.encode()) < 100_000
    assert context['review_location']['path'] == 'target.py'
    assert context['review_location']['line'] == 17
    assert context['review_location']['diff_hunk'] == '@@ target'
    assert len(context['review_conversation']) == 2
    assert any('#42 unavailable' in item for item in context['missing_context'])
    if not large:
        assert context['linked_issues'] == [{'body': 'available requirements'}]
        assert context['source'] == {'target.py': 'target source'}
    assert sum('/contents/' in args.args[1] for args in source._api.await_args_list) == 1
    assert source._api.await_count == 8


def test_answers_require_explicit_tool_free_provider():
    async def run():
        provider = SimpleNamespace(connect=AsyncMock())
        reply = AsyncMock()
        concierge = GithubConcierge(provider=provider, continue_workorder=AsyncMock(),
                                     reply=reply, context=AsyncMock(return_value='evidence'))
        request = FeedbackRequest(RunOrigin(channel='github:acme/api', thread_id='7', author='reviewer'), 'Why?')
        with pytest.raises(RuntimeError, match='tool-free'):
            await concierge.handle(request)
        provider.connect.assert_not_awaited()
        reply.assert_not_awaited()
    asyncio.run(run())


def test_general_context_limits_source_fetches_and_skips_oversized_files():
    import json
    from engine.adapters.source_control.github import GitHubSourceControl

    source = GitHubSourceControl('')
    async def api(method, path):
        assert method == 'GET'
        if '/files?' in path:
            return [{'filename': f'{index}.py'} for index in range(500)]
        if '/commits?' in path:
            return []
        if '/contents/' in path:
            return {'encoding': 'base64', 'content': 'a' * 24_001}
        return {'body': '', 'head': {'sha': 'abc'}}
    source._api = AsyncMock(side_effect=api)
    context = json.loads(asyncio.run(source.review_context('https://github.com/acme/api/pull/7')))
    assert context['source'] == {}
    assert sum('/contents/' in args.args[1] for args in source._api.await_args_list) == 3
    assert sum('exceeds 18 KB' in item for item in context['missing_context']) == 3

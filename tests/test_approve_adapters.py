"""Approval adapters use the forge's approval endpoint, never a merge endpoint."""
import asyncio
import json

import httpx
import pytest

from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.source_control.gitlab import GitLabSourceControl


@pytest.mark.parametrize('forge', ['github', 'gitlab'])
def test_approval_http_request(monkeypatch, forge):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 1})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    source = GitHubSourceControl('token') if forge == 'github' else GitLabSourceControl('token')
    project = 'owner/repo' if forge == 'github' else 'gitlab.com/group/nested/repo'
    asyncio.run(source.approve_change_request(project, 42, 'Green: covered by tests.', commit_sha='reviewed-sha'))
    assert all(request.method == 'POST' for request in requests)
    if forge == 'github':
        assert len(requests) == 1
        assert str(requests[0].url) == 'https://api.github.com/repos/owner/repo/pulls/42/reviews'
        assert json.loads(requests[0].content) == {'event': 'APPROVE', 'body': 'Green: covered by tests.', 'commit_id': 'reviewed-sha'}
    else:
        assert len(requests) == 2
        assert requests[0].url.raw_path == b'/api/v4/projects/group%2Fnested%2Frepo/merge_requests/42/notes'
        assert json.loads(requests[0].content) == {'body': 'Green: covered by tests.'}
        assert json.loads(requests[1].content) == {'sha': 'reviewed-sha'}
        assert requests[1].url.raw_path == b'/api/v4/projects/group%2Fnested%2Frepo/merge_requests/42/approve'


@pytest.mark.parametrize('forge', ['github', 'gitlab'])
def test_approval_rejects_foreign_project(forge):
    source = GitHubSourceControl('token') if forge == 'github' else GitLabSourceControl('token')
    with pytest.raises(ValueError):
        asyncio.run(source.approve_change_request('foreign.example/owner/repo', 42, 'Green', commit_sha='reviewed-sha'))


@pytest.mark.parametrize('provider,project', [
    ('gh-cli', 'owner/repo'),
    ('github-oauth', 'owner/repo'),
    ('gitlab-oauth', 'gitlab.com/group/repo'),
])
def test_routing_preserves_reviewed_sha(tmp_path, provider, project):
    from unittest.mock import AsyncMock

    from engine.apps.web.source_control import RoutingSourceControl, SourceControlPreferences

    preferences = SourceControlPreferences(tmp_path / 'settings.json')
    preferences.set(provider)
    providers = [AsyncMock(), AsyncMock(), AsyncMock()]
    source = RoutingSourceControl(preferences, *providers)
    asyncio.run(source.approve_change_request(project, 42, 'Green', commit_sha='reviewed-sha'))
    selected = providers[['gh-cli', 'github-oauth', 'gitlab-oauth'].index(provider)]
    selected.approve_change_request.assert_awaited_once_with(
        project, 42, 'Green', commit_sha='reviewed-sha',
    )

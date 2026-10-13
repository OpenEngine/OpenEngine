"""Approval requests pass through the real adapters and fake HTTP transports."""
import asyncio
import json

import httpx
import pytest

from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.source_control.gitlab import GitLabSourceControl


@pytest.mark.parametrize("forge", ["github", "gitlab"])
@pytest.mark.parametrize("self_authored", [False, True])
def test_approval_http(monkeypatch, forge, self_authored):
    requests = []
    login_key = "login" if forge == "github" else "username"
    author_key = "user" if forge == "github" else "author"

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={login_key: "review-bot"})
        if request.method == "GET":
            return httpx.Response(200, json={author_key: {
                login_key: "Review-Bot" if self_authored else "contributor",
            }})
        return httpx.Response(200, json={"id": 7})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    if forge == "github":
        source, project = GitHubSourceControl("token"), "owner/repo"
        root = "/repos/owner/repo/pulls/42"
    else:
        source, project = GitLabSourceControl("token"), "gitlab.com/group/nested/repo"
        root = "/api/v4/projects/group%2Fnested%2Frepo/merge_requests/42"
    body = "Approved automatically because impact analysis rated this change Green.\n\nTypo fix."
    assert asyncio.run(source.approve_change_request(project, 42, body, commit_sha="reviewed-sha")) is not self_authored
    posts = [r for r in requests if r.method == "POST"]
    if self_authored:
        assert posts == []
    elif forge == "github":
        assert len(posts) == 1
        assert posts[0].url.path == root + "/reviews"
        assert json.loads(posts[0].content) == {"event": "APPROVE", "body": body, "commit_id": "reviewed-sha"}
    else:
        assert [r.url.raw_path.decode() for r in posts] == [root + "/approve", root + "/notes"]
        assert json.loads(posts[0].content) == {"sha": "reviewed-sha"}
        assert json.loads(posts[1].content) == {"body": body}


@pytest.mark.parametrize("forge", ["github", "gitlab"])
def test_approval_refuses_unknown_author(monkeypatch, forge):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"login": "bot", "username": "bot"})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    source = GitHubSourceControl("token") if forge == "github" else GitLabSourceControl("token")
    project = "owner/repo" if forge == "github" else "gitlab.com/group/repo"
    with pytest.raises(RuntimeError, match="no .* author"):
        asyncio.run(source.approve_change_request(project, 42, "Green", commit_sha="reviewed-sha"))
    assert not any(r.method == "POST" for r in requests)


def test_gitlab_failed_approval_does_not_post_success_note(monkeypatch):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"username": "bot"})
        if request.method == "GET":
            return httpx.Response(200, json={"author": {"username": "contributor"}})
        return httpx.Response(403, json={"message": "Forbidden"})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    with pytest.raises(RuntimeError):
        asyncio.run(GitLabSourceControl("token").approve_change_request(
            "gitlab.com/group/repo", 42, "Approved automatically", commit_sha="reviewed-sha",
        ))
    assert [r.url.path.rsplit("/", 1)[-1] for r in requests if r.method == "POST"] == ["approve"]

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from engine.adapters.source_control.gitlab.transports import (
    GitLabOAuthTransport,
    GitLabTransportError,
)
from engine.adapters.source_control.gitlab import GitLabSourceControl


class _Client:
    responses: list[httpx.Response] = []
    requests: list[httpx.Request] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def request(self, method: str, url: str, **kwargs):
        request = httpx.Request(method, url, headers=kwargs["headers"])
        self.requests.append(request)
        response = self.responses.pop(0)
        response.request = request
        return response


def test_gitlab_transport_refreshes_once_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    current = {"token": "old"}

    async def refresh(failed: str) -> bool:
        assert failed == "old"
        current["token"] = "new"
        return True

    _Client.responses = [httpx.Response(401), httpx.Response(200, json={"id": 1})]
    _Client.requests = []
    monkeypatch.setattr(
        "engine.adapters.source_control.gitlab.transports.httpx.AsyncClient", _Client
    )

    transport = GitLabOAuthTransport(lambda: current["token"], on_token_unauthorized=refresh)
    assert asyncio.run(transport.request("GET", "/projects/1")) == {"id": 1}
    assert [request.headers["authorization"] for request in _Client.requests] == [
        "Bearer old",
        "Bearer new",
    ]


def test_gitlab_transport_does_not_retry_a_second_unauthorized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    refresh = AsyncMock(return_value=True)
    _Client.responses = [httpx.Response(401), httpx.Response(401, json={"message": "bad token"})]
    _Client.requests = []
    monkeypatch.setattr(
        "engine.adapters.source_control.gitlab.transports.httpx.AsyncClient", _Client
    )

    with pytest.raises(GitLabTransportError, match="bad token"):
        asyncio.run(GitLabOAuthTransport("old", on_token_unauthorized=refresh).request("GET", "/user"))
    refresh.assert_awaited_once_with("old")


def test_gitlab_inline_comment_uses_a_positioned_discussion() -> None:
    class Transport:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            if method == "GET":
                return {"diff_refs": {"base_sha": "base", "start_sha": "start", "head_sha": "head"}}
            return {"notes": [{"id": 123}]}

    transport = Transport()
    source = GitLabSourceControl("token", transport=transport)  # type: ignore[arg-type]
    result = asyncio.run(source.add_comment("https://gitlab.com/group/project/-/merge_requests/7", "Fix this", "src/app.py", 12))

    assert result.id == 123
    assert result.url == "https://gitlab.com/group/project/-/merge_requests/7#note_123"
    assert transport.calls[1] == (
        "POST",
        "/projects/group%2Fproject/merge_requests/7/discussions",
        {"json": {"body": "Fix this", "position": {"position_type": "text", "base_sha": "base", "start_sha": "start", "head_sha": "head", "new_path": "src/app.py", "new_line": 12}}},
    )


def test_gitlab_general_comment_returns_provenance() -> None:
    transport = AsyncMock()
    transport.request.return_value = {"id": 124}
    source = GitLabSourceControl("token", transport=transport)
    result = asyncio.run(source.add_comment("https://gitlab.com/group/project/-/merge_requests/7", "Hello"))
    assert result.id == 124
    assert result.url == "https://gitlab.com/group/project/-/merge_requests/7#note_124"


def test_gitlab_replies_fail_without_posting_a_flat_comment() -> None:
    transport = AsyncMock()
    source = GitLabSourceControl("token", transport=transport)
    with pytest.raises(NotImplementedError, match="replies"):
        asyncio.run(source.add_comment("https://gitlab.com/group/project/-/merge_requests/7", "Reply", in_reply_to_id=123))
    transport.request.assert_not_awaited()


@pytest.mark.parametrize(
    "mr_url",
    [
        # Refused by the shared reader, so refused here too.
        "https://gitlab.com/group/project/-/merge_requests/\u0661\u0662",
        "https://gitlab.com/group/project/-/merge_requests/\u00b2",
        "https://gitlab.com/group/project/-/merge_requests/042",
        "https://gitlab.com/x/y/pull/7/-/merge_requests/1",
        "https://gitlab.com/a/b/-/merge_requests/1/-/merge_requests/2",
        "https://gitlab.com/x/y/pull/7",
        # Quoting escapes the separators between project steps but leaves a
        # `.` alone, so a bare `..` would be resolved away before sending.
        "https://gitlab.com/../-/merge_requests/1",
        "https://gitlab.com/../x/-/merge_requests/1",
        "https://gitlab.com/a/../../x/-/merge_requests/1",
        "https://gitlab.com/./x/-/merge_requests/1",
    ],
)
def test_a_url_the_shared_reader_refuses_is_not_sent_to_gitlab(mr_url: str) -> None:
    transport = AsyncMock()
    source = GitLabSourceControl("token", transport=transport)
    with pytest.raises(ValueError, match="merge-request URL"):
        asyncio.run(source.add_comment(mr_url, "Finding."))
    transport.request.assert_not_awaited()


@pytest.mark.parametrize(
    "mr_url, parts",
    [
        ("https://gitlab.com/group/sub.one/project/-/merge_requests/7", ("group%2Fsub.one%2Fproject", 7)),
        # The reader's key is lowercased; the path the API is asked about is not.
        ("https://gitlab.com/Group/Sub/Project/-/merge_requests/7#note_1", ("Group%2FSub%2FProject", 7)),
        # Copied off the Changes tab, and read past as CICheck reads past it.
        ("https://gitlab.com/group/project/-/merge_requests/7/diffs", ("group%2Fproject", 7)),
        ("https://gitlab.com/group/project/-/merge_requests/7/", ("group%2Fproject", 7)),
    ],
)
def test_a_merge_request_is_read_by_the_shared_reader(mr_url: str, parts: tuple[str, int]) -> None:
    assert GitLabSourceControl._merge_request(mr_url) == parts


@pytest.mark.parametrize(
    "remote, project",
    [
        ("https://gitlab.example/..", None),
        ("https://gitlab.example/group/../x.git", None),
        ("git@gitlab.example:./x.git", None),
        ("https://gitlab.example/group/sub.one/project.git", "group%2Fsub.one%2Fproject"),
        ("git@gitlab.example:group/project.git", "group%2Fproject"),
    ],
)
def test_a_project_read_off_the_remote_is_names_and_not_moves(
    monkeypatch: pytest.MonkeyPatch, remote: str, project: str | None
) -> None:
    from engine.adapters.source_control.gitlab import GitLabSourceControlError

    source = GitLabSourceControl("token", transport=AsyncMock())

    async def checked(_workspace: object, _arguments: object) -> str:
        return remote

    monkeypatch.setattr(source, "_checked", checked)
    if project is None:
        with pytest.raises(GitLabSourceControlError, match="cannot determine the project"):
            asyncio.run(source._project("ws"))  # type: ignore[arg-type]
    else:
        assert asyncio.run(source._project("ws")) == project  # type: ignore[arg-type]


@pytest.mark.parametrize("suffix", ["", ":8443"])
def test_gitlab_comments_stay_on_the_current_configured_origin(suffix):
    origin = {"url": "https://gitlab.example" + suffix}
    transport = AsyncMock()
    transport.request.return_value = {"id": 1}
    source = GitLabSourceControl("", origin=lambda: origin["url"], transport=transport)
    for host in ["evil.example", "gitlab.com", "gitlab.example:9443"]:
        with pytest.raises(ValueError, match="configured GitLab origin"):
            asyncio.run(source.add_comment(f"https://{host}/group/project/-/merge_requests/7", "Hello"))
    transport.request.assert_not_awaited()
    asyncio.run(source.add_comment(origin["url"] + "/group/project/-/merge_requests/7", "Hello"))
    transport.request.assert_awaited_once()
    origin["url"] = "https://other.example"
    with pytest.raises(ValueError, match="configured GitLab origin"):
        asyncio.run(source.add_comment("https://gitlab.example" + suffix + "/group/project/-/merge_requests/7", "Hello"))

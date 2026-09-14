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
        # `str.isdigit` is true of each, and `int` raises on the second.
        "https://gitlab.com/group/project/-/merge_requests/١٢",
        "https://gitlab.com/group/project/-/merge_requests/²",
        "https://gitlab.com/group/project/-/merge_requests/042",
    ],
)
def test_a_merge_request_number_is_spelled_one_way(mr_url: str) -> None:
    """The same one spelling the GitHub adapter and the runtime hold to.

    A `pr_url` travels between them, so a number one reads and another does
    not is how a run's work ends up bound to a change request it never touched.
    """
    source = GitLabSourceControl("token", transport=AsyncMock())
    with pytest.raises(ValueError, match="merge-request URL"):
        asyncio.run(source.add_comment(mr_url, "Finding."))


@pytest.mark.parametrize(
    "mr_url",
    [
        # The project comes out of the path and goes to the configured GitLab,
        # so an unchecked host is a disguise, not a destination: this is a note
        # on victim/project!1 written by this deployment's own token.
        "https://evil.example/victim/project/-/merge_requests/1",
        "https://gitlab.com.evil.example/victim/project/-/merge_requests/1",
        "https://github.com/victim/project/-/merge_requests/1",
    ],
)
def test_a_note_goes_only_to_the_gitlab_this_deployment_talks_to(mr_url: str) -> None:
    """The same hole the GitHub adapter had, closed the same way.

    There is no separate allowlist here because the origin an operator already
    configured is the answer: this adapter talks to one GitLab.
    """
    transport = AsyncMock()
    source = GitLabSourceControl("token", transport=transport)
    with pytest.raises(ValueError, match="merge-request URL"):
        asyncio.run(source.add_comment(mr_url, "Finding."))
    transport.request.assert_not_awaited()


def test_a_self_hosted_gitlab_is_named_by_the_origin_it_was_given() -> None:
    """Including when the origin is resolved per request, as the web app does."""
    transport = AsyncMock()
    transport.request.return_value = {"id": 1}
    origin = "https://gitlab.acme.com"
    source = GitLabSourceControl("token", origin=lambda: origin, transport=transport)
    url = "https://gitlab.acme.com/group/project/-/merge_requests/7"
    asyncio.run(source.add_comment(url, "Finding."))
    transport.request.assert_awaited_once()
    with pytest.raises(ValueError, match="merge-request URL"):
        asyncio.run(
            source.add_comment(
                "https://gitlab.com/group/project/-/merge_requests/7", "Finding."
            )
        )


@pytest.mark.parametrize(
    "mr_url",
    [
        # Quoting escapes the separators between project steps but leaves a
        # `.` alone, so a bare `..` survives into `/projects/../merge_requests`
        # and is resolved away before the request leaves.
        "https://gitlab.com/../-/merge_requests/1",
        "https://gitlab.com/../x/-/merge_requests/1",
        "https://gitlab.com/a/../../x/-/merge_requests/1",
        "https://gitlab.com/./x/-/merge_requests/1",
    ],
)
def test_a_project_path_is_names_and_not_moves(mr_url: str) -> None:
    """The same defect the GitHub adapter had, closed by the same rule."""
    transport = AsyncMock()
    source = GitLabSourceControl("token", transport=transport)
    with pytest.raises(ValueError, match="merge-request URL"):
        asyncio.run(source.add_comment(mr_url, "Finding."))
    transport.request.assert_not_awaited()


def test_a_nested_project_is_still_read() -> None:
    """A GitLab project nests to any depth, and a dot inside a step is a name."""
    from engine.adapters.source_control.gitlab import GitLabSourceControl as GL

    source = GL("token", transport=AsyncMock())
    assert source._merge_request(
        "https://gitlab.com/group/sub.one/project/-/merge_requests/7"
    ) == ("group%2Fsub.one%2Fproject", 7)

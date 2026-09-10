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
    class Transport:
        request = AsyncMock(return_value={"id": 456})

    transport = Transport()
    source = GitLabSourceControl("token", transport=transport)  # type: ignore[arg-type]
    result = asyncio.run(source.add_comment("https://gitlab.com/group/project/-/merge_requests/7", "Looks good."))
    assert result.id == 456
    assert result.url == "https://gitlab.com/group/project/-/merge_requests/7#note_456"
    transport.request.assert_awaited_once_with("POST", "/projects/group%2Fproject/merge_requests/7/notes", json={"body": "Looks good."})


def test_gitlab_reply_is_explicitly_unsupported() -> None:
    with pytest.raises(NotImplementedError, match="replies are not supported"):
        asyncio.run(GitLabSourceControl("token").add_comment(
            "https://gitlab.com/group/project/-/merge_requests/7", "Fixed.", in_reply_to_id=123,
        ))

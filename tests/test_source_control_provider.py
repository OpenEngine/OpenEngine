"""Provider preference, first-run selection, and GH CLI transport tests."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from engine.adapters.source_control.github.transports import (
    GitHubCliTransport,
    GitHubOAuthTransport,
    GitHubTransportError,
)
from engine.apps.web.source_control import (
    GhCliStatus,
    RoutingSourceControl,
    SourceControlPreferences,
    gh_cli_status,
    selected_or_detected_provider,
)


def test_first_run_selects_and_persists_an_authenticated_cli(tmp_path: Path) -> None:
    preferences = SourceControlPreferences(tmp_path / "settings.json")

    selected, automatic = selected_or_detected_provider(
        preferences, lambda: GhCliStatus(True, True, account="octocat")
    )

    assert (selected, automatic) == ("gh-cli", True)
    assert preferences.get() == "gh-cli"

    selected, automatic = selected_or_detected_provider(
        preferences, lambda: pytest.fail("saved choice must prevent detection")
    )
    assert (selected, automatic) == ("gh-cli", False)


def test_cli_status_reports_the_authenticated_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_process = MagicMock(returncode=0, stdout=b"github.com\n", stderr=b"")
    user_process = MagicMock(returncode=0, stdout=b"octocat\n", stderr=b"")
    processes = [status_process, user_process]
    monkeypatch.setattr(
        "engine.apps.web.source_control.subprocess.run",
        lambda *_args, **_kwargs: processes.pop(0),
    )

    status = gh_cli_status()

    assert status == GhCliStatus(
        True, True, account="octocat", message="GitHub CLI is authenticated"
    )


@pytest.mark.parametrize(
    "status",
    [GhCliStatus(False, False), GhCliStatus(True, False)],
)
def test_first_run_uses_oauth_without_an_authenticated_cli(
    tmp_path: Path, status: GhCliStatus
) -> None:
    preferences = SourceControlPreferences(tmp_path / "settings.json")

    selected, automatic = selected_or_detected_provider(preferences, lambda: status)

    assert (selected, automatic) == ("github-oauth", True)


def test_cli_transport_encodes_json_on_stdin_not_in_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = GitHubCliTransport()
    seen: dict[str, object] = {}

    async def run(*arguments: str, input_bytes: bytes | None = None) -> bytes:
        seen["arguments"] = arguments
        seen["input"] = input_bytes
        return b'{"html_url":"https://github.com/acme/api/pull/1"}'

    monkeypatch.setattr(transport, "_run", run)
    result = asyncio.run(
        transport.request(
            "POST",
            "/repos/acme/api/pulls",
            json={"title": "quote ' and newline\\n"},
        )
    )

    assert result == {"html_url": "https://github.com/acme/api/pull/1"}
    assert "quote ' and newline" not in " ".join(seen["arguments"])
    assert seen["input"] == b'{"title": "quote \' and newline\\\\n"}'


def test_cli_transport_turns_missing_binary_into_actionable_error() -> None:
    transport = GitHubCliTransport("definitely-not-a-gh-binary")

    with pytest.raises(GitHubTransportError, match="not installed"):
        asyncio.run(transport.request("GET", "/user"))


def test_oauth_transport_refreshes_once_after_401_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        httpx.Response(401, json={"message": "Bad credentials"}),
        httpx.Response(200, json={"login": "octocat"}),
    ]
    authorizations: list[str] = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def request(self, _method: str, _url: str, **kwargs: object):
            headers = kwargs["headers"]
            assert isinstance(headers, dict)
            authorizations.append(headers.get("Authorization", ""))
            return responses.pop(0)

    current_token = {"value": "old"}

    async def refresh(failed_token: str) -> bool:
        assert failed_token == "old"
        current_token["value"] = "new"
        return True

    monkeypatch.setattr(
        "engine.adapters.source_control.github.transports.httpx.AsyncClient", Client
    )
    transport = GitHubOAuthTransport(
        lambda: current_token["value"], on_token_unauthorized=refresh
    )
    assert asyncio.run(transport.request("GET", "/user")) == {"login": "octocat"}
    assert authorizations == ["Bearer old", "Bearer new"]


def test_oauth_transport_does_not_retry_a_second_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        httpx.Response(401, json={"message": "Bad credentials"}),
        httpx.Response(401, json={"message": "Still bad"}),
    ]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

        async def request(self, *_: object, **__: object):
            return responses.pop(0)

    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "engine.adapters.source_control.github.transports.httpx.AsyncClient", Client
    )
    transport = GitHubOAuthTransport("old", on_token_unauthorized=refresh)
    with pytest.raises(GitHubTransportError, match="Still bad"):
        asyncio.run(transport.request("GET", "/user"))
    refresh.assert_awaited_once_with("old")


def test_router_identifies_the_provider_in_a_source_control_failure(
    tmp_path: Path,
) -> None:
    class FailingSourceControl:
        async def add_comment(self, *_: object) -> None:
            raise RuntimeError("GitHub CLI is not authenticated")

    preferences = SourceControlPreferences(tmp_path / "settings.json")
    preferences.set("gh-cli")
    router = RoutingSourceControl(
        preferences,
        FailingSourceControl(),  # type: ignore[arg-type]
        FailingSourceControl(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="GH CLI provider failed"):
        asyncio.run(router.add_comment("https://github.com/acme/api/pull/1", "Hello"))

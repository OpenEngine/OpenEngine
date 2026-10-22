"""The user's source-control preference and dynamic provider router."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar
from urllib.parse import urlsplit

from engine.adapters.source_control.github.transports import gh_cli_environment
from engine.apps.web.settings_file import atomic_write_json
from engine.domain.ids import WorkspaceId
from engine.ports.source_control import (
    ChangeRequest,
    Discussion,
    CommentResult,
    GitResult,
    JobLogs,
    PipelineRetry,
    PipelineStatus,
    SourceControl,
    WorkItem,
)
from engine.ports.workspace_provider import WorkspaceProvider
from platformdirs import user_config_path

SourceControlProvider = Literal["gh-cli", "github-oauth", "gitlab-oauth"]
_PROVIDERS = frozenset({"gh-cli", "github-oauth", "gitlab-oauth"})
_Result = TypeVar("_Result")

#: How long the GitHub CLI may take to report its authentication state. It
#: talks to github.com, so an unreachable forge would otherwise hold a caller
#: open for as long as the network does.
_STATUS_TIMEOUT_SECONDS = 10

_PROVIDER_NAMES = {"gh-cli": "GH CLI", "github-oauth": "GitHub OAuth", "gitlab-oauth": "GitLab OAuth"}
_DEFAULT_GITLAB_ORIGIN = "https://gitlab.com"


class SourceControlPreferences:
    """Small non-secret, per-user preference store.

    This deliberately does not share OAuth's keychain: a selected integration
    is configuration, not a credential. Atomic replacement prevents a partial
    settings file if the process stops while writing.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or user_config_path("openengine") / "settings.json"

    def get(self) -> SourceControlProvider | None:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8")).get(
                "sourceControlProvider"
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return value if value in _PROVIDERS else None

    def set(self, provider: SourceControlProvider, gitlab_origin: str | None = None) -> None:
        if provider not in _PROVIDERS:
            raise ValueError(f"unsupported source-control provider: {provider}")
        value: dict[str, str] = {"sourceControlProvider": provider}
        if gitlab_origin:
            value["gitlabOrigin"] = gitlab_origin
        atomic_write_json(self._path, value)

    def gitlab_origin(self) -> str | None:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8")).get("gitlabOrigin")
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None
        return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class GhCliStatus:
    installed: bool
    authenticated: bool
    account: str = ""
    message: str = ""


def gh_cli_status(binary_path: str = "gh") -> GhCliStatus:
    """Return an actionable status without ever prompting for authentication."""
    try:
        process = subprocess.run(
            [binary_path, "auth", "status", "--hostname", "github.com"],
            capture_output=True,
            check=False,
            env=gh_cli_environment(),
            timeout=_STATUS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return GhCliStatus(False, False, message="GitHub CLI is not installed")
    except subprocess.TimeoutExpired:
        # These calls reach github.com, so they are as slow as the network is:
        # an unauthenticated answer beats blocking a caller indefinitely.
        return GhCliStatus(True, False, message="GitHub CLI did not answer in time")
    except OSError as error:
        return GhCliStatus(False, False, message=f"Could not start GitHub CLI: {error}")
    if process.returncode:
        return GhCliStatus(
            True, False, message="GitHub CLI is not authenticated; run 'gh auth login'"
        )
    try:
        user = subprocess.run(
            [binary_path, "api", "user", "--jq", ".login"],
            capture_output=True,
            check=False,
            env=gh_cli_environment(),
            timeout=_STATUS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        # The account name is a nicety; authentication is already established.
        user = None
    account = (
        user.stdout.decode(errors="replace").strip()
        if user and not user.returncode
        else ""
    )
    return GhCliStatus(
        True, True, account=account, message="GitHub CLI is authenticated"
    )


def selected_or_detected_provider(
    preferences: SourceControlPreferences,
    status: Callable[[], GhCliStatus] | None = None,
) -> tuple[SourceControlProvider, bool]:
    """Return the saved choice, or persist first-run CLI detection exactly once."""
    selected = preferences.get()
    if selected is not None:
        return selected, False
    detected = (status or gh_cli_status)()
    selected = (
        "gh-cli" if detected.installed and detected.authenticated else "github-oauth"
    )
    preferences.set(selected)
    return selected, True


class RoutingSourceControl:
    """Select one provider once per port operation, so a running call is stable."""

    def __init__(
        self,
        preferences: SourceControlPreferences,
        gh_cli: SourceControl,
        github_oauth: SourceControl,
        gitlab_oauth: SourceControl | None = None,
        *,
        workspace_provider: WorkspaceProvider | None = None,
        github_host_aliases: Mapping[str, str] | None = None,
        github_oauth_connected: Callable[[], bool] | None = None,
    ) -> None:
        self._preferences = preferences
        self._providers = {"gh-cli": gh_cli, "github-oauth": github_oauth}
        if gitlab_oauth is not None:
            self._providers["gitlab-oauth"] = gitlab_oauth
        # Without a workspace provider the router cannot read a remote, so
        # workspace operations keep following the saved choice alone.
        self._workspace_provider = workspace_provider
        self._github_hosts = frozenset({"github.com"}) | frozenset(
            _host(alias)
            for alias, target in (github_host_aliases or {}).items()
            if _host(target) == "github.com"
        )
        self._github_oauth_connected = github_oauth_connected

    def _selected(self) -> tuple[SourceControlProvider, SourceControl]:
        selected = self._preferences.get()
        if selected is None:
            selected, _ = selected_or_detected_provider(self._preferences)
        return selected, self._providers[selected]

    async def _github_provider(
        self, selected: SourceControlProvider
    ) -> SourceControlProvider | None:
        """The saved GitHub choice, or detection without saving a choice."""
        if selected != "gitlab-oauth":
            return selected
        # Detection shells out to the GitHub CLI, so it runs off the event
        # loop: a slow `gh` must not stall every other request this process
        # is serving.
        status = await asyncio.to_thread(gh_cli_status)
        if status.installed and status.authenticated:
            return "gh-cli"
        if self._github_oauth_connected is None or self._github_oauth_connected():
            return "github-oauth"
        return None

    async def _workspace_provider_for(
        self, workspace_id: WorkspaceId, selected: SourceControlProvider
    ) -> SourceControlProvider:
        """Follow the forge of the workspace's `origin`, not one global choice."""
        assert self._workspace_provider is not None
        root = await self._workspace_provider.root_path(workspace_id)
        host = _host(await _origin_url(root))
        gitlab_origin = self._preferences.gitlab_origin() or _DEFAULT_GITLAB_ORIGIN
        provider: SourceControlProvider | None
        if host in self._github_hosts:
            provider = await self._github_provider(selected)
        elif host and host == _host(gitlab_origin):
            provider = "gitlab-oauth" if "gitlab-oauth" in self._providers else None
        elif host and selected == "gitlab-oauth":
            # An unrecognised host is never handed to the configured GitLab.
            provider = None
        else:
            provider = selected
        if provider is None:
            configured = _PROVIDER_NAMES[selected].removesuffix(" OAuth")
            if selected == "gitlab-oauth":
                configured = f"{configured}, {gitlab_origin}"
            raise RuntimeError(
                f"workspace remote {host} does not match the selected source "
                f"control ({configured})"
            )
        return provider

    async def _call(
        self, operation: Callable[[SourceControl], Awaitable[_Result]],
        *, pr_url: str = "", workspace_id: WorkspaceId | None = None,
    ) -> _Result:
        provider, source_control = self._selected()
        if workspace_id is not None and self._workspace_provider is not None:
            provider = await self._workspace_provider_for(workspace_id, provider)
            source_control = self._providers[provider]
        elif provider == "gitlab-oauth" and urlsplit(pr_url).hostname == "github.com":
            # Webhook targets are independent of the workspace provider choice.
            # Use the normal GitHub credential detection without saving a choice.
            status = await asyncio.to_thread(gh_cli_status)
            provider = "gh-cli" if status.installed and status.authenticated else "github-oauth"
            source_control = self._providers[provider]
        try:
            return await operation(source_control)
        except RuntimeError as error:
            raise RuntimeError(f"{_PROVIDER_NAMES[provider]} provider failed: {error}") from error

    async def run_git(
        self, workspace_id: WorkspaceId, arguments: Sequence[str],
        *, owned_pull_requests: Sequence[tuple[str, int]] = (),
    ) -> GitResult:
        return await self._call(
            lambda source: source.run_git(
                workspace_id, arguments, owned_pull_requests=owned_pull_requests,
            ),
            workspace_id=workspace_id,
        )

    async def create_branch(
        self, workspace_id: WorkspaceId, name: str, base_ref: str
    ) -> None:
        await self._call(
            lambda source: source.create_branch(workspace_id, name, base_ref),
            workspace_id=workspace_id,
        )

    async def commit_all(self, workspace_id: WorkspaceId, message: str) -> str:
        return await self._call(
            lambda source: source.commit_all(workspace_id, message),
            workspace_id=workspace_id,
        )

    async def publish(self, workspace_id: WorkspaceId, branch: str) -> None:
        await self._call(
            lambda source: source.publish(workspace_id, branch),
            workspace_id=workspace_id,
        )

    async def request_review(
        self,
        workspace_id: WorkspaceId,
        branch: str,
        base_ref: str,
        title: str,
        body: str,
        *, issue: dict[str, object] | None = None, issue_resolution: str | None = None,
        owned_pull_requests: Sequence[tuple[str, int]] = (),
    ) -> str:
        return await self._call(
            lambda source: source.request_review(
                workspace_id, branch, base_ref, title, body,
                owned_pull_requests=owned_pull_requests,
                **({"issue": issue, "issue_resolution": issue_resolution} if issue else {}),
            ),
            workspace_id=workspace_id,
        )

    async def approve_change_request(self, project: str, number: int, body: str, *, commit_sha: str) -> None:
        url = f"https://github.com/{project}" if project.count('/') == 1 else f"https://{project}"
        await self._call(lambda source: source.approve_change_request(project, number, body, commit_sha=commit_sha), pr_url=url)

    async def can_write_repository(
        self, pr_url: str, username: str, *, user_id: int | None = None
    ) -> bool:
        if user_id is None:
            call = lambda source: source.can_write_repository(pr_url, username)
        else:
            call = lambda source: source.can_write_repository(pr_url, username, user_id=user_id)
        return await self._call(call, pr_url=pr_url)

    async def authenticated_login(self, repository_url: str) -> str:
        return await self._call(
            lambda source: source.authenticated_login(repository_url),
            pr_url=repository_url,
        )

    async def add_reaction(
        self, pr_url: str, comment_id: int, content: str, *, review_comment: bool = False,
    ) -> None:
        await self._call(
            lambda source: source.add_reaction(
                pr_url, comment_id, content, review_comment=review_comment,
            ),
            pr_url=pr_url,
        )

    async def add_comment(
        self,
        pr_url: str,
        comment: str,
        file: str | None = None,
        line: int | None = None,
        in_reply_to_id: int | None = None,
        *, thread_id: str | None = None, resolve: bool = False, commit_sha: str | None = None,
    ) -> CommentResult:
        return await self._call(
            lambda source: source.add_comment(pr_url, comment, file, line, in_reply_to_id,
                **({"thread_id": thread_id, "resolve": resolve, "commit_sha": commit_sha} if thread_id is not None or resolve or commit_sha is not None else {})),
            pr_url=pr_url,
        )

    async def review_thread(self, pr_url: str, comment_id: int) -> Discussion:
        return await self._call(lambda source: source.review_thread(pr_url, comment_id), pr_url=pr_url)

    async def resolve_review_thread(self, pr_url: str, thread_id: str) -> bool:
        return await self._call(lambda source: source.resolve_review_thread(pr_url, thread_id), pr_url=pr_url)

    async def inspect_target(self, project: str, *, number: int = 0, branch: str = "", include_threads: bool = False) -> dict[str, object]:
        async def inspect(source: SourceControl) -> dict[str, object]:
            return await getattr(source, "inspect_target")(project, number=number, branch=branch, include_threads=include_threads)
        try:
            return await self._call(inspect, pr_url=f"https://github.com/{project}")
        except (RuntimeError, OSError):
            from engine.adapters.source_control.github import GitHubSourceControl
            # Metadata for public repositories is readable without a connection.
            return await GitHubSourceControl("").inspect_target(
                project, number=number, branch=branch, include_threads=include_threads)

    async def view_change_request(
        self, workspace_id: WorkspaceId, number: int
    ) -> ChangeRequest:
        return await self._call(
            lambda source: source.view_change_request(workspace_id, number),
            workspace_id=workspace_id,
        )

    async def list_work_items(
        self,
        workspace_id: WorkspaceId,
        state: str = "open",
        labels: Sequence[str] = (),
        limit: int = 30,
    ) -> tuple[WorkItem, ...]:
        return await self._call(
            lambda source: source.list_work_items(workspace_id, state, labels, limit),
            workspace_id=workspace_id,
        )

    async def view_work_item(self, workspace_id: WorkspaceId, number: int) -> WorkItem:
        return await self._call(
            lambda source: source.view_work_item(workspace_id, number),
            workspace_id=workspace_id,
        )

    async def list_pipeline_status(
        self,
        workspace_id: WorkspaceId,
        *,
        ref: str | None = None,
        change_request_number: int | None = None,
    ) -> PipelineStatus:
        return await self._call(
            lambda source: source.list_pipeline_status(
                workspace_id, ref=ref, change_request_number=change_request_number
            ),
            workspace_id=workspace_id,
        )

    async def get_job_logs(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> JobLogs:
        return await self._call(
            lambda source: source.get_job_logs(workspace_id, pipeline_id, job_id),
            workspace_id=workspace_id,
        )

    async def retry_pipeline(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> PipelineRetry:
        return await self._call(
            lambda source: source.retry_pipeline(workspace_id, pipeline_id, job_id),
            workspace_id=workspace_id,
        )


def _host(location: str) -> str:
    """The lowercase host of a URL, scp-style remote or bare authority."""
    location = location.strip()
    if "://" not in location:
        # `git@github.com:owner/repo.git` and `alias.example:8443` alike.
        location = "ssh://" + location.replace(":", "/", 1) if "@" in location else f"https://{location}"
    try:
        return (urlsplit(location).hostname or "").lower()
    except ValueError:
        return ""


async def _origin_url(root_path: str) -> str:
    """The workspace's `origin` URL, or empty when it has none."""
    process = await asyncio.create_subprocess_exec(
        "git", "-C", root_path, "remote", "get-url", "origin",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    return stdout.decode(errors="replace").strip() if process.returncode == 0 else ""


__all__ = [
    "GhCliStatus",
    "RoutingSourceControl",
    "SourceControlPreferences",
    "SourceControlProvider",
    "gh_cli_status",
    "selected_or_detected_provider",
]

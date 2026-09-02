"""The user's source-control preference and dynamic provider router."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from engine.domain.ids import WorkspaceId
from engine.ports.source_control import (
    ChangeRequest,
    GitResult,
    JobLogs,
    PipelineRetry,
    PipelineStatus,
    SourceControl,
    WorkItem,
)
from platformdirs import user_config_path

SourceControlProvider = Literal["gh-cli", "github-oauth"]
_PROVIDERS = frozenset({"gh-cli", "github-oauth"})
_Result = TypeVar("_Result")


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

    def set(self, provider: SourceControlProvider) -> None:
        if provider not in _PROVIDERS:
            raise ValueError(f"unsupported source-control provider: {provider}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"sourceControlProvider": provider}) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self._path)


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
        )
    except FileNotFoundError:
        return GhCliStatus(False, False, message="GitHub CLI is not installed")
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
        )
    except OSError:
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
    status: Callable[[], GhCliStatus] = gh_cli_status,
) -> tuple[SourceControlProvider, bool]:
    """Return the saved choice, or persist first-run CLI detection exactly once."""
    selected = preferences.get()
    if selected is not None:
        return selected, False
    detected = status()
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
    ) -> None:
        self._preferences = preferences
        self._providers = {"gh-cli": gh_cli, "github-oauth": github_oauth}

    def _selected(self) -> tuple[SourceControlProvider, SourceControl]:
        selected = self._preferences.get()
        if selected is None:
            selected, _ = selected_or_detected_provider(self._preferences)
        return selected, self._providers[selected]

    async def _call(
        self, operation: Callable[[SourceControl], Awaitable[_Result]]
    ) -> _Result:
        provider, source_control = self._selected()
        try:
            return await operation(source_control)
        except RuntimeError as error:
            name = "GH CLI" if provider == "gh-cli" else "GitHub OAuth"
            raise RuntimeError(f"{name} provider failed: {error}") from error

    async def run_git(
        self, workspace_id: WorkspaceId, arguments: Sequence[str]
    ) -> GitResult:
        return await self._call(lambda source: source.run_git(workspace_id, arguments))

    async def create_branch(
        self, workspace_id: WorkspaceId, name: str, base_ref: str
    ) -> None:
        await self._call(
            lambda source: source.create_branch(workspace_id, name, base_ref)
        )

    async def commit_all(self, workspace_id: WorkspaceId, message: str) -> str:
        return await self._call(lambda source: source.commit_all(workspace_id, message))

    async def publish(self, workspace_id: WorkspaceId, branch: str) -> None:
        await self._call(lambda source: source.publish(workspace_id, branch))

    async def request_review(
        self,
        workspace_id: WorkspaceId,
        branch: str,
        base_ref: str,
        title: str,
        body: str,
    ) -> str:
        return await self._call(
            lambda source: source.request_review(
                workspace_id, branch, base_ref, title, body
            )
        )

    async def add_comment(
        self,
        pr_url: str,
        comment: str,
        file: str | None = None,
        line: int | None = None,
    ) -> None:
        await self._call(lambda source: source.add_comment(pr_url, comment, file, line))

    async def view_change_request(
        self, workspace_id: WorkspaceId, number: int
    ) -> ChangeRequest:
        return await self._call(
            lambda source: source.view_change_request(workspace_id, number)
        )

    async def list_work_items(
        self,
        workspace_id: WorkspaceId,
        state: str = "open",
        labels: Sequence[str] = (),
        limit: int = 30,
    ) -> tuple[WorkItem, ...]:
        return await self._call(
            lambda source: source.list_work_items(workspace_id, state, labels, limit)
        )

    async def view_work_item(self, workspace_id: WorkspaceId, number: int) -> WorkItem:
        return await self._call(
            lambda source: source.view_work_item(workspace_id, number)
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
            )
        )

    async def get_job_logs(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> JobLogs:
        return await self._call(
            lambda source: source.get_job_logs(workspace_id, pipeline_id, job_id)
        )

    async def retry_pipeline(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> PipelineRetry:
        return await self._call(
            lambda source: source.retry_pipeline(workspace_id, pipeline_id, job_id)
        )


__all__ = [
    "GhCliStatus",
    "RoutingSourceControl",
    "SourceControlPreferences",
    "SourceControlProvider",
    "gh_cli_status",
    "selected_or_detected_provider",
]

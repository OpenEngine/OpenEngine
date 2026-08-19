"""GitHub source control, with review comments backed by the user's ``gh`` CLI."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from urllib.parse import urlparse

from engine.domain.ids import WorkspaceId


class GitHubSourceControl:
    """Branches, commits, pushes, and pull requests against GitHub.

    Implements `engine.ports.SourceControl`.
    """

    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        binary_path: str = "gh",
    ) -> None:
        self._token = token
        self._api_url = api_url
        self._binary_path = binary_path

    async def create_branch(self, workspace_id: WorkspaceId, name: str, base_ref: str) -> None:
        raise NotImplementedError("GitHub branching lands with the source-control ticket")

    async def commit_all(self, workspace_id: WorkspaceId, message: str) -> str:
        raise NotImplementedError("GitHub commits land with the source-control ticket")

    async def publish(self, workspace_id: WorkspaceId, branch: str) -> None:
        raise NotImplementedError("GitHub push lands with the source-control ticket")

    async def request_review(
        self, repository: str, branch: str, base_ref: str, title: str, body: str
    ) -> str:
        raise NotImplementedError("GitHub pull requests land with the source-control ticket")

    async def add_comment(
        self,
        pr_url: str,
        comment: str,
        file: str | None = None,
        line: int | None = None,
    ) -> None:
        """Add a general or inline pull-request comment with ``gh``."""

        if not pr_url.strip():
            raise ValueError("pr_url must not be empty")
        if not comment.strip():
            raise ValueError("comment must not be empty")
        if file is not None and not file.strip():
            raise ValueError("file must not be empty")
        if (file is None) != (line is None):
            raise ValueError("file and line must be provided together")
        if line is not None and (
            not isinstance(line, int) or isinstance(line, bool) or line < 1
        ):
            raise ValueError("line must be a positive integer")

        pull_request = _pull_request_parts(pr_url)
        if file is None:
            await self._gh("pr", "comment", pr_url, "--body", comment)
            return

        owner, repository, number = pull_request
        head_sha = await self._gh(
            "pr", "view", pr_url, "--json", "headRefOid", "--jq", ".headRefOid"
        )
        if not head_sha:
            raise GitHubSourceControlError("gh returned an empty pull-request head SHA")
        await self._gh(
            "api",
            "--method",
            "POST",
            f"repos/{owner}/{repository}/pulls/{number}/comments",
            "--raw-field",
            f"body={comment}",
            "--raw-field",
            f"commit_id={head_sha}",
            "--raw-field",
            f"path={file}",
            "--field",
            f"line={line}",
            "--raw-field",
            "side=RIGHT",
        )

    async def _gh(self, *arguments: str) -> str:
        environment: Mapping[str, str] | None = None
        if self._token:
            environment = {**os.environ, "GH_TOKEN": self._token}
        return await _run_gh(self._binary_path, arguments, environment)


class GitHubSourceControlError(RuntimeError):
    """The GitHub CLI could not perform a source-control operation."""


def _pull_request_parts(pr_url: str) -> tuple[str, str, str]:
    parsed = urlparse(pr_url)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or len(parts) != 4
        or parts[2] != "pull"
        or not parts[3].isdigit()
    ):
        raise ValueError("pr_url must be a GitHub pull-request URL")
    return parts[0], parts[1], parts[3]


async def _run_gh(
    binary_path: str,
    arguments: tuple[str, ...],
    environment: Mapping[str, str] | None,
) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            binary_path,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        raise GitHubSourceControlError(f"could not start {binary_path}: {error}") from error
    stdout, stderr = await process.communicate()
    if process.returncode:
        detail = stderr.decode(errors="replace").strip() or "unknown error"
        raise GitHubSourceControlError(f"gh failed: {detail}")
    return stdout.decode(errors="replace").strip()


__all__ = ["GitHubSourceControl", "GitHubSourceControlError"]

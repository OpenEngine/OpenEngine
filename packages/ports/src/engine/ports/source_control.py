"""Source Control capability.

Branches, commits, and review requests. Deliberately phrased in neutral terms
("changes", "review") rather than GitHub's ("PR") so a GitLab or plain-git
implementation does not have to lie about what it is doing.
"""

from typing import Protocol, runtime_checkable

from engine.domain.ids import WorkspaceId


@runtime_checkable
class SourceControl(Protocol):
    """Publishes work produced in a workspace and opens it for review."""

    async def create_branch(self, workspace_id: WorkspaceId, name: str, base_ref: str) -> None:
        ...

    async def commit_all(self, workspace_id: WorkspaceId, message: str) -> str:
        """Commit the workspace's current state. Returns the commit SHA."""
        ...

    async def publish(self, workspace_id: WorkspaceId, branch: str) -> None:
        """Push the branch to the remote."""
        ...

    async def request_review(
        self, repository: str, branch: str, base_ref: str, title: str, body: str
    ) -> str:
        """Open a review (pull request). Returns its URL."""
        ...

    async def add_comment(
        self,
        pr_url: str,
        comment: str,
        file: str | None = None,
        line: int | None = None,
    ) -> None:
        """Comment on a review, optionally at a line in a changed file."""
        ...


__all__ = ["SourceControl"]

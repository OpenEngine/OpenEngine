"""Source Control capability, backed by GitHub.

Placeholder for Ticket 1. Satisfies `engine.ports.SourceControl` structurally;
no HTTP client and no authentication yet.
"""

from engine.domain.ids import WorkspaceId


class GitHubSourceControl:
    """Branches, commits, pushes, and pull requests against GitHub.

    Implements `engine.ports.SourceControl`.
    """

    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self._token = token
        self._api_url = api_url

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


__all__ = ["GitHubSourceControl"]

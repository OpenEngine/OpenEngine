"""Source Control capability.

Branches, commits, and review requests. Deliberately phrased in neutral terms
("changes", "review") rather than GitHub's ("PR") so a GitLab or plain-git
implementation does not have to lie about what it is doing.

`run_git` is the odd one out, and deliberately so. Git is already the
vocabulary an agent thinks in, and a fixed menu of named methods would put this
port in the business of deciding, one ticket at a time, which of git's several
hundred subcommands an agent is allowed to reach -- a decision it would get
wrong in the direction of "not yet". So the named methods stay for the
operations *Engine itself* issues, and everything else goes through as an
argument vector the implementation runs.

What that bounds is the workspace, not the subcommand: the caller still names
only a `WorkspaceId`, so an agent can run any git it likes and only ever in the
checkout it was given.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from engine.domain.ids import WorkspaceId


@dataclass(frozen=True, slots=True)
class GitResult:
    """What one git invocation printed, and what it exited with.

    A non-zero exit is reported rather than raised, because for git it is not
    reliably an error: `diff --exit-code`, `merge-base --is-ancestor` and
    `check-ignore` all answer their question that way. Raising on all of them
    would make a whole class of git unreachable through this port; the caller
    that asked the question is the one that knows whether the answer is bad
    news.
    """

    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@runtime_checkable
class SourceControl(Protocol):
    """Publishes work produced in a workspace and opens it for review."""

    async def run_git(
        self, workspace_id: WorkspaceId, arguments: Sequence[str]
    ) -> GitResult:
        """Run `git` with these arguments inside the workspace's checkout.

        `arguments` is an argument vector, not a command line: no shell is
        involved, so nothing here is quoted, split, or expanded, and a commit
        message with newlines in it is simply one element.
        """
        ...

    async def create_branch(self, workspace_id: WorkspaceId, name: str, base_ref: str) -> None:
        ...

    async def commit_all(self, workspace_id: WorkspaceId, message: str) -> str:
        """Commit the workspace's current state. Returns the commit SHA."""
        ...

    async def publish(self, workspace_id: WorkspaceId, branch: str) -> None:
        """Push the branch to the remote."""
        ...

    async def request_review(
        self,
        workspace_id: WorkspaceId,
        branch: str,
        base_ref: str,
        title: str,
        body: str,
    ) -> str:
        """Open a review (pull request). Returns its URL.

        Keyed by workspace like everything else here: which repository this is
        against is written in the checkout's own remote, and a caller that had
        to name it separately would be a caller that could name a different
        one.
        """
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


__all__ = ["GitResult", "SourceControl"]

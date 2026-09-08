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


@dataclass(frozen=True, slots=True)
class Discussion:
    """One comment or review on a forge change request or work item."""

    author: str
    body: str
    url: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    """A pull request, merge request, or equivalent proposed change."""

    number: int
    title: str
    state: str
    body: str
    author: str
    url: str
    head_ref: str
    head_sha: str
    base_ref: str
    reviews: tuple[Discussion, ...] = ()
    comments: tuple[Discussion, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkItem:
    """A forge issue or equivalent tracked unit of work."""

    number: int
    title: str
    state: str
    body: str
    author: str
    url: str
    labels: tuple[str, ...] = ()
    comments: tuple[Discussion, ...] = ()


@dataclass(frozen=True, slots=True)
class StatusCheck:
    """One provider status/check associated with a revision."""

    name: str
    status: str
    conclusion: str | None
    details_url: str


@dataclass(frozen=True, slots=True)
class Pipeline:
    """One provider CI pipeline/workflow run."""

    pipeline_id: int
    name: str
    status: str
    conclusion: str | None
    url: str


@dataclass(frozen=True, slots=True)
class PipelineStatus:
    """Checks and CI pipelines for one revision."""

    ref: str
    checks: tuple[StatusCheck, ...]
    pipelines: tuple[Pipeline, ...]


@dataclass(frozen=True, slots=True)
class JobLogs:
    """A bounded text excerpt of one CI job's logs."""

    pipeline_id: int
    job_id: int
    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class PipelineRetry:
    """What CI job or pipeline a retry request was accepted for."""

    pipeline_id: int
    job_id: int | None = None


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

    async def view_change_request(
        self, workspace_id: WorkspaceId, number: int
    ) -> ChangeRequest:
        """Return a change request and its reviews and discussions."""
        ...

    async def list_work_items(
        self,
        workspace_id: WorkspaceId,
        state: str = "open",
        labels: Sequence[str] = (),
        limit: int = 30,
    ) -> tuple[WorkItem, ...]:
        """List work items in the workspace repository."""
        ...

    async def view_work_item(self, workspace_id: WorkspaceId, number: int) -> WorkItem:
        """Return one work item and its discussion."""
        ...

    async def list_pipeline_status(
        self,
        workspace_id: WorkspaceId,
        *,
        ref: str | None = None,
        change_request_number: int | None = None,
    ) -> PipelineStatus:
        """Return provider checks and CI pipelines for a ref or change request."""
        ...

    async def get_job_logs(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> JobLogs:
        """Return a bounded log excerpt for a CI job in a pipeline."""
        ...

    async def retry_pipeline(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> PipelineRetry:
        """Retry a CI pipeline, or one job and its dependents when supported."""
        ...


__all__ = [
    "ChangeRequest",
    "Discussion",
    "GitResult",
    "JobLogs",
    "Pipeline",
    "PipelineRetry",
    "PipelineStatus",
    "SourceControl",
    "StatusCheck",
    "WorkItem",
]

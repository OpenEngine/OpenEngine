"""GitHub source control: git locally and a selectable GitHub API transport.

Two mechanisms, one capability. `git` handles everything that happens inside a
checkout (branching, committing, pushing) and `httpx` handles everything that
talks to github.com (pull requests, comments). Which one a method uses is an
implementation detail of that method.

The adapter is composed with the workspace provider because every operation is
keyed by `WorkspaceId` and git needs a directory. Resolving it here rather than
letting callers pass a path is what keeps the engine from ever holding one.
"""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlparse

from engine.adapters.source_control.github.transports import (
    GitHubApiTransport,
    GitHubOAuthTransport,
    GitHubTransportError,
)
from engine.domain.ids import WorkspaceId
from engine.ports.source_control import (
    ChangeRequest,
    Discussion,
    GitResult,
    JobLogs,
    Pipeline,
    PipelineRetry,
    PipelineStatus,
    StatusCheck,
    WorkItem,
)
from engine.ports.workspace_provider import WorkspaceProvider

#: The branch prefix `GitWorktreeWorkspaceProvider` gives every workspace. It
#: is Engine's bookkeeping, not anybody's proposed change, and a remote branch
#: named after it is a leak of the internals into somebody's repository -- so
#: publishing one is refused here rather than asked for in a prompt, which is
#: the difference between a rule and a suggestion.
INTERNAL_BRANCH_PREFIX = "engine/"

#: Git's own options -- the ones before a subcommand -- that this tool will
#: pass on. An allowlist rather than a list of refusals, because the options
#: worth refusing cannot be enumerated: `-c` alone reaches `alias.*`,
#: `core.pager`, `core.sshCommand`, `diff.external`, `credential.helper` and
#: every other config key whose value git runs as a program, and the next
#: release may add another. Naming what passes is a rule that stays true.
#:
#: Everything here changes how git reads its own arguments or writes its own
#: output, and none of it runs a program or chooses a repository. `--help` is
#: absent for that reason: it hands off to a man viewer.
_PERMITTED_GLOBAL_OPTIONS = frozenset(
    {
        "-P",
        "--no-pager",
        "--no-advice",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-replace-objects",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--version",
    }
)

#: `git push` options that consume the argument after them. Needed only so a
#: value like `--receive-pack /usr/bin/git-receive-pack` is not mistaken for a
#: refspec while working out what a push would actually create.
_PUSH_OPTIONS_TAKING_A_VALUE = frozenset(
    {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}
)

#: `git push` options that push every local branch rather than a named one, so
#: the argument vector names no destination and the refs do.
_PUSH_OPTIONS_TAKING_EVERY_BRANCH = frozenset(
    {"--all", "--branches", "--mirror"}
)

#: The refspecs that mean "the branch that is checked out" rather than naming
#: one. `git push origin HEAD` creates a remote branch named after the current
#: one, which is a name only the checkout knows.
_CHECKED_OUT_REFSPECS = frozenset({"HEAD", "@"})

#: A push whose target is inferred from configuration or the checked-out ref
#: cannot be proved not to publish Engine's branch. Agents can express every
#: ordinary publish explicitly (`agent/topic` or `HEAD:agent/topic`), so the
#: adapter refuses the ambiguous spellings instead of trying to reproduce
#: git's configuration-dependent refspec resolution.
_AMBIGUOUS_PUSH_REFSPECS = frozenset({":", "+:"})
_MAX_LOG_BYTES = 8 * 1024 * 1024
_MAX_LOG_CHARACTERS = 48_000


class GitHubSourceControl:
    """Branches, commits, pushes, and pull requests against GitHub.

    Implements `engine.ports.SourceControl`.
    """

    def __init__(
        self,
        token: str | Callable[[], str | None],
        api_url: str = "https://api.github.com",
        workspace_provider: WorkspaceProvider | None = None,
        git_binary_path: str = "git",
        transport: GitHubApiTransport | None = None,
    ) -> None:
        self._transport = transport or GitHubOAuthTransport(token, api_url)
        self._workspace_provider = workspace_provider
        self._git_binary_path = git_binary_path

    async def run_git(
        self, workspace_id: WorkspaceId, arguments: Sequence[str]
    ) -> GitResult:
        """Run any git subcommand inside one workspace's checkout.

        The broker obtains approval before this method is called. The adapter
        still owns two hard invariants: global options may not redirect git's
        implementation, and a push must explicitly name a non-internal remote
        branch.
        """

        arguments = tuple(str(argument) for argument in arguments)
        if not arguments:
            raise ValueError("git needs at least one argument")
        subcommand = _subcommand_index(arguments)
        root_path = await self._root_path(workspace_id)
        if subcommand is not None and arguments[subcommand] == "push":
            self._refuse_internal_publication(arguments[subcommand:])
        return await self._git(root_path, arguments)

    async def create_branch(
        self, workspace_id: WorkspaceId, name: str, base_ref: str
    ) -> None:
        await self._git_checked(
            await self._root_path(workspace_id), ("checkout", "-b", name, base_ref)
        )

    async def commit_all(self, workspace_id: WorkspaceId, message: str) -> str:
        root_path = await self._root_path(workspace_id)
        await self._git_checked(root_path, ("add", "--all"))
        await self._git_checked(root_path, ("commit", "--message", message))
        return await self._git_checked(root_path, ("rev-parse", "HEAD"))

    async def publish(self, workspace_id: WorkspaceId, branch: str) -> None:
        root_path = await self._root_path(workspace_id)
        _refuse_internal_branch(branch)
        await self._git_checked(root_path, ("push", "--set-upstream", "origin", branch))

    async def request_review(
        self,
        workspace_id: WorkspaceId,
        branch: str,
        base_ref: str,
        title: str,
        body: str,
    ) -> str:
        """Open a pull request via the GitHub API and return its URL."""

        if not branch.strip():
            raise ValueError("branch must not be empty")
        if not title.strip():
            raise ValueError("title must not be empty")
        _refuse_internal_branch(branch)

        root_path = await self._root_path(workspace_id)
        owner, repo = await self._repo_coords(root_path)
        base = _base_branch(base_ref)

        response = _object(await self._api(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": branch, "base": base},
        ))
        url = response.get("html_url", "")
        if not url:
            raise GitHubSourceControlError("GitHub API returned no pull-request URL")
        return url

    async def add_comment(
        self,
        pr_url: str,
        comment: str,
        file: str | None = None,
        line: int | None = None,
    ) -> None:
        """Add a general or inline pull-request comment via the GitHub API."""

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

        owner, repo, number = _pull_request_parts(pr_url)

        if file is None:
            await self._api(
                "POST",
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                json={"body": comment},
            )
            return

        # Inline comment: resolve the PR head SHA first.
        pr_data = await self._api("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        head_sha = pr_data.get("head", {}).get("sha", "")
        if not head_sha:
            raise GitHubSourceControlError(
                "GitHub API returned an empty pull-request head SHA"
            )
        await self._api(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/comments",
            json={
                "body": comment,
                "commit_id": head_sha,
                "path": file,
                "line": line,
                "side": "RIGHT",
            },
        )

    async def view_change_request(
        self, workspace_id: WorkspaceId, number: int
    ) -> ChangeRequest:
        owner, repo = await self._workspace_repo(workspace_id)
        number = _positive_number(number, "number")
        pull = _object(await self._api("GET", f"/repos/{owner}/{repo}/pulls/{number}"))
        reviews = await self._paginated_objects(f"/repos/{owner}/{repo}/pulls/{number}/reviews")
        issue_comments = await self._paginated_objects(
            f"/repos/{owner}/{repo}/issues/{number}/comments"
        )
        inline_comments = await self._paginated_objects(
            f"/repos/{owner}/{repo}/pulls/{number}/comments"
        )
        return ChangeRequest(
            number=number,
            title=_string(pull, "title"),
            state=_string(pull, "state"),
            body=_string(pull, "body"),
            author=_nested_string(pull, "user", "login"),
            url=_string(pull, "html_url"),
            head_ref=_nested_string(pull, "head", "ref"),
            head_sha=_nested_string(pull, "head", "sha"),
            base_ref=_nested_string(pull, "base", "ref"),
            reviews=tuple(_discussion(review) for review in reviews),
            comments=tuple(_discussion(comment) for comment in (*issue_comments, *inline_comments)),
        )

    async def list_work_items(
        self,
        workspace_id: WorkspaceId,
        state: str = "open",
        labels: Sequence[str] = (),
        limit: int = 30,
    ) -> tuple[WorkItem, ...]:
        owner, repo = await self._workspace_repo(workspace_id)
        state = _work_item_state(state)
        limit = _limit(limit)
        data = await self._paginated_objects(
            f"/repos/{owner}/{repo}/issues",
            {"state": state, "labels": ",".join(labels)},
        )
        return tuple(
            _work_item(issue)
            for issue in data
            if not isinstance(issue.get("pull_request"), dict)
        )[:limit]

    async def view_work_item(self, workspace_id: WorkspaceId, number: int) -> WorkItem:
        owner, repo = await self._workspace_repo(workspace_id)
        number = _positive_number(number, "number")
        issue = _object(await self._api("GET", f"/repos/{owner}/{repo}/issues/{number}"))
        comments = await self._paginated_objects(f"/repos/{owner}/{repo}/issues/{number}/comments")
        return _work_item(issue, tuple(_discussion(comment) for comment in comments))

    async def list_pipeline_status(
        self,
        workspace_id: WorkspaceId,
        *,
        ref: str | None = None,
        change_request_number: int | None = None,
    ) -> PipelineStatus:
        owner, repo = await self._workspace_repo(workspace_id)
        ref = await self._status_ref(owner, repo, ref, change_request_number)
        checks = await self._paginated_field(f"/repos/{owner}/{repo}/commits/{ref}/check-runs", "check_runs")
        runs = await self._paginated_field(f"/repos/{owner}/{repo}/actions/runs", "workflow_runs", {"head_sha": ref})
        return PipelineStatus(
            ref=ref,
            checks=tuple(
                StatusCheck(
                    name=_string(check, "name"),
                    status=_string(check, "status"),
                    conclusion=_optional_string(check, "conclusion"),
                    details_url=_string(check, "details_url"),
                )
                for check in checks
            ),
            pipelines=tuple(
                Pipeline(
                    pipeline_id=_positive_number(run.get("id"), "workflow run id"),
                    name=_string(run, "name"),
                    status=_string(run, "status"),
                    conclusion=_optional_string(run, "conclusion"),
                    url=_string(run, "html_url"),
                )
                for run in runs
            ),
        )

    async def get_job_logs(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> JobLogs:
        owner, repo = await self._workspace_repo(workspace_id)
        pipeline_id = _positive_number(pipeline_id, "pipeline_id")
        if job_id is None:
            jobs = _object(
                await self._api("GET", f"/repos/{owner}/{repo}/actions/runs/{pipeline_id}/jobs")
            )
            candidates = _objects(jobs.get("jobs", []))
            failed = [job for job in candidates if job.get("conclusion") == "failure"]
            completed = [job for job in candidates if job.get("status") == "completed"]
            if not (failed or completed):
                raise GitHubSourceControlError(
                    "GitHub returned no completed jobs for workflow run"
                )
            job_id = _positive_number((failed or completed)[0].get("id"), "job_id")
        else:
            job_id = _positive_number(job_id, "job_id")
        content = await self._download(f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs")
        text = _log_text(content)
        truncated = len(text) > _MAX_LOG_CHARACTERS
        return JobLogs(
            pipeline_id=pipeline_id,
            job_id=job_id,
            text=text[-_MAX_LOG_CHARACTERS:] if truncated else text,
            truncated=truncated,
        )

    async def retry_pipeline(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> PipelineRetry:
        owner, repo = await self._workspace_repo(workspace_id)
        pipeline_id = _positive_number(pipeline_id, "pipeline_id")
        if job_id is None:
            await self._api("POST", f"/repos/{owner}/{repo}/actions/runs/{pipeline_id}/rerun")
            return PipelineRetry(pipeline_id=pipeline_id)
        job_id = _positive_number(job_id, "job_id")
        await self._api("POST", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/rerun")
        return PipelineRetry(pipeline_id=pipeline_id, job_id=job_id)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _root_path(self, workspace_id: WorkspaceId) -> str:
        if self._workspace_provider is None:
            raise GitHubSourceControlError(
                "this source control was composed without a workspace provider, "
                "so it cannot find the checkout to work in"
            )
        return await self._workspace_provider.root_path(workspace_id)

    async def _workspace_repo(self, workspace_id: WorkspaceId) -> tuple[str, str]:
        return await self._repo_coords(await self._root_path(workspace_id))

    async def _status_ref(
        self, owner: str, repo: str, ref: str | None, change_request_number: int | None
    ) -> str:
        if (ref is None) == (change_request_number is None):
            raise ValueError("provide exactly one of ref or change_request_number")
        if ref is not None:
            if not ref.strip():
                raise ValueError("ref must not be empty")
            return ref
        number = _positive_number(change_request_number, "change_request_number")
        pull = _object(await self._api("GET", f"/repos/{owner}/{repo}/pulls/{number}"))
        sha = _nested_string(pull, "head", "sha")
        if not sha:
            raise GitHubSourceControlError("GitHub API returned an empty pull-request head SHA")
        return sha

    def _refuse_internal_publication(self, arguments: Sequence[str]) -> None:
        """Stop a push before it puts an Engine branch on somebody's remote."""

        destinations = _push_destinations(arguments)
        for destination in destinations:
            _refuse_internal_branch(destination)

    async def _git(self, root_path: str, arguments: Sequence[str]) -> GitResult:
        try:
            process = await asyncio.create_subprocess_exec(
                self._git_binary_path,
                "-C",
                root_path,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._git_environment(),
            )
        except OSError as error:
            raise GitHubSourceControlError(
                f"could not start {self._git_binary_path}: {error}"
            ) from error
        stdout, stderr = await process.communicate()
        return GitResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace").strip(),
            stderr=stderr.decode(errors="replace").strip(),
        )

    async def _git_checked(self, root_path: str, arguments: Sequence[str]) -> str:
        result = await self._git(root_path, arguments)
        if not result.ok:
            detail = result.stderr or result.stdout or "unknown error"
            raise GitHubSourceControlError(
                f"git {arguments[0]} failed: {detail}"
            )
        return result.stdout

    def _git_environment(self) -> Mapping[str, str]:
        """The host environment without forge bearer tokens.

        Git authentication belongs to the configured credential helper. A git
        subprocess does not need the token used for the GitHub API, and git can
        invoke helpers, hooks and aliases, so putting that token in its
        environment turns any such program into a credential reader.
        """

        return {
            name: value
            for name, value in os.environ.items()
            if name
            not in {
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "GH_ENTERPRISE_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN",
            }
        }

    async def _api(self, method: str, path: str, **kwargs: object) -> object:
        """Make one GitHub request, preserving one adapter-level error shape."""
        try:
            return await self._transport.request(method, path, **kwargs)
        except GitHubTransportError as error:
            raise GitHubSourceControlError(str(error)) from error

    async def _paginated_objects(self, path: str, params: Mapping[str, object] | None = None) -> tuple[dict, ...]:
        """Read every GitHub list page rather than silently accepting page one."""

        items: list[dict] = []
        page = 1
        while True:
            current = _objects(
                await self._api("GET", path, params={"per_page": 100, "page": page, **(params or {})})
            )
            items.extend(current)
            if len(current) < 100:
                return tuple(items)
            page += 1

    async def _paginated_field(self, path: str, field: str, params: Mapping[str, object] | None = None) -> tuple[dict, ...]:
        items: list[dict] = []
        page = 1
        while True:
            data = _object(await self._api("GET", path, params={"per_page": 100, "page": page, **(params or {})}))
            current = _objects(data.get(field, []))
            items.extend(current)
            if len(items) >= data.get("total_count", 0) or len(current) < 100:
                return tuple(items)
            page += 1

    async def _download(self, path: str) -> bytes:
        try:
            content = await self._transport.download(path)
        except GitHubTransportError as error:
            raise GitHubSourceControlError(str(error)) from error
        if len(content) > _MAX_LOG_BYTES:
            raise GitHubSourceControlError("GitHub job logs exceed the download limit")
        return content

    async def _repo_coords(self, root_path: str) -> tuple[str, str]:
        """The `owner/repo` pair from the workspace's `origin` remote URL."""
        result = await self._git_checked(
            root_path, ("remote", "get-url", "origin")
        )
        return _parse_repo_coords(result)


class GitHubSourceControlError(RuntimeError):
    """The GitHub API could not perform a source-control operation."""


class InternalBranchPublicationError(GitHubSourceControlError):
    """Something tried to publish Engine's own bookkeeping branch."""

    def __init__(self, branch: str) -> None:
        super().__init__(
            f"{branch} is an internal Engine branch and must not be published\n"
            f"hint: create a descriptive branch such as agent/<description> from "
            f"the intended base, apply only the commits meant for review, and "
            f"push that instead"
        )
        self.branch = branch


class UnsafePushSpecificationError(InternalBranchPublicationError):
    """A push leaves its destination to git configuration or bulk expansion."""

    def __init__(self, refspec: str) -> None:
        GitHubSourceControlError.__init__(
            self,
            f"push target {refspec!r} is not explicit enough to prove that it "
            "excludes Engine's internal branch; name a concrete destination "
            "such as agent/<description> or HEAD:agent/<description>",
        )
        self.branch = refspec


class GitOutsideWorkspaceError(GitHubSourceControlError):
    """A git command tried to point itself at a different repository."""

    def __init__(self, option: str) -> None:
        super().__init__(
            f"{option} would run git somewhere other than this workspace, which "
            f"is the one thing this tool does not do"
        )
        self.option = option


class GitGlobalOptionError(GitOutsideWorkspaceError):
    """A global option could change what executable git runs."""

    def __init__(self, option: str) -> None:
        GitHubSourceControlError.__init__(
            self,
            f"git global option {option} is not available through git_subcommand; "
            "pass an ordinary git subcommand and its arguments instead",
        )
        self.option = option


def _refuse_internal_branch(branch: str) -> None:
    if _branch_name(branch).startswith(INTERNAL_BRANCH_PREFIX):
        raise InternalBranchPublicationError(branch)


def _branch_name(ref: str) -> str:
    """The branch a ref names, with the decoration git allows around one."""
    return ref.lstrip("+").removeprefix("refs/heads/")


def _subcommand_index(arguments: Sequence[str]) -> int | None:
    """Where the subcommand sits, rejecting executable-selecting options.

    Git's global option surface is security-sensitive: `-c alias.x=!sh` and
    `--exec-path` both select programs before a subcommand begins. Permit only
    value-free presentation/pathspec switches whose meaning is closed here.
    """
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-"):
            return index
        option = argument.partition("=")[0]
        if option not in _PERMITTED_GLOBAL_OPTIONS:
            raise GitGlobalOptionError(option)
        index += 1
    return None


def _push_destinations(arguments: Sequence[str]) -> tuple[str, ...]:
    """The branches a `git push` argument vector would write to.

    Only explicit, concrete destinations pass. Git otherwise consults the
    checked-out branch and `remote.*.push`, while bulk and wildcard forms can
    publish refs absent from argv. Reimplementing that resolver incompletely is
    exactly how an internal branch escaped the original guard.
    """
    positional: list[str] = []
    skip_next = False
    repository_from_option = False
    for argument in arguments[1:]:
        if skip_next:
            skip_next = False
            continue
        if argument.startswith("-"):
            option = argument.partition("=")[0]
            if option in _PUSH_OPTIONS_TAKING_EVERY_BRANCH:
                raise UnsafePushSpecificationError(option)
            skip_next = "=" not in argument and option in _PUSH_OPTIONS_TAKING_A_VALUE
            repository_from_option = repository_from_option or option == "--repo"
            continue
        positional.append(argument)

    # Ordinarily the first positional is the remote. `--repo=<remote>` supplies
    # it as an option instead, making every positional a refspec.
    refspecs = positional if repository_from_option else positional[1:]
    if not refspecs:
        # `--tags` does not publish a branch. Every other refspec-free push is
        # configuration-dependent and therefore not provably safe.
        if any(argument.partition("=")[0] == "--tags" for argument in arguments[1:]):
            return ()
        raise UnsafePushSpecificationError("implicit push refspec")

    destinations: list[str] = []
    for refspec in refspecs:
        undecorated = refspec.lstrip("+")
        if undecorated in _AMBIGUOUS_PUSH_REFSPECS or "*" in undecorated:
            raise UnsafePushSpecificationError(refspec)
        source, separator, destination = undecorated.partition(":")
        if not separator:
            if source in _CHECKED_OUT_REFSPECS:
                raise UnsafePushSpecificationError(source)
            destination = source
        elif not destination:
            raise UnsafePushSpecificationError(refspec)
        destinations.append(_branch_name(destination))
    return tuple(destinations)


def _base_branch(base_ref: str) -> str:
    """The branch `base_ref` names, as GitHub wants it written.

    Only `origin/` comes off, and only as a prefix: a base of `release/2.0` is
    a branch whose name has a slash in it, and stripping up to the first one
    would quietly propose against `2.0` instead.
    """
    return base_ref.removeprefix("origin/")


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


def _parse_repo_coords(remote_url: str) -> tuple[str, str]:
    """Extract `(owner, repo)` from an HTTPS or SSH remote URL.

    Handles the two common spellings:
      https://github.com/owner/repo.git
      git@github.com:owner/repo.git
    """
    remote_url = remote_url.strip()
    # SSH shorthand: git@github.com:owner/repo.git
    if remote_url.startswith("git@"):
        path = remote_url.split(":", 1)[-1]
    else:
        parsed = urlparse(remote_url)
        path = parsed.path
    path = path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) < 2:
        raise GitHubSourceControlError(
            f"cannot determine owner/repo from remote URL: {remote_url!r}"
        )
    return parts[0], parts[1]


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise GitHubSourceControlError("GitHub API returned an unexpected response")
    return value


def _objects(value: object) -> tuple[dict, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise GitHubSourceControlError("GitHub API returned an unexpected list response")
    return tuple(value)


def _string(value: dict, name: str) -> str:
    item = value.get(name, "")
    return item if isinstance(item, str) else ""


def _optional_string(value: dict, name: str) -> str | None:
    item = value.get(name)
    return item if isinstance(item, str) else None


def _nested_string(value: dict, outer: str, inner: str) -> str:
    nested = value.get(outer)
    return _string(nested, inner) if isinstance(nested, dict) else ""


def _discussion(value: dict) -> Discussion:
    line = value.get("line")
    return Discussion(
        author=_nested_string(value, "user", "login"),
        body=_string(value, "body"),
        url=_string(value, "html_url"),
        path=_optional_string(value, "path"),
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
    )


def _work_item(value: dict, comments: tuple[Discussion, ...] = ()) -> WorkItem:
    labels = value.get("labels", [])
    names = tuple(
        _string(label, "name") for label in labels if isinstance(label, dict) and _string(label, "name")
    )
    return WorkItem(
        number=_positive_number(value.get("number"), "issue number"),
        title=_string(value, "title"),
        state=_string(value, "state"),
        body=_string(value, "body"),
        author=_nested_string(value, "user", "login"),
        url=_string(value, "html_url"),
        labels=names,
        comments=comments,
    )


def _positive_number(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    return value


def _work_item_state(value: object) -> str:
    if value not in {"open", "closed", "all"}:
        raise ValueError("state must be open, closed, or all")
    return str(value)


def _log_text(content: bytes) -> str:
    if content.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                return "\n".join(
                    archive.read(name).decode(errors="replace")
                    for name in archive.namelist()
                    if not name.endswith("/")
                )
        except (OSError, zipfile.BadZipFile) as error:
            raise GitHubSourceControlError(f"could not read GitHub job log archive: {error}") from error
    return content.decode(errors="replace")


__all__ = [
    "INTERNAL_BRANCH_PREFIX",
    "GitGlobalOptionError",
    "GitHubSourceControl",
    "GitHubSourceControlError",
    "GitOutsideWorkspaceError",
    "InternalBranchPublicationError",
    "UnsafePushSpecificationError",
]

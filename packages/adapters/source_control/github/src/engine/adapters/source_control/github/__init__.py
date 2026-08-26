"""GitHub source control: git in the workspace, ``gh`` for everything else.

Two binaries, one capability. `git` does the work that happens inside a
checkout and `gh` the work that happens on github.com, and which of the two a
method reaches for is an implementation detail of that method rather than
something a caller picks.

The adapter is composed with the workspace provider because every operation
here is keyed by `WorkspaceId` and git needs a directory. Resolving it here
rather than letting callers pass a path is what keeps the engine from ever
holding one -- the same reason `CodexAgentRunner` takes the provider.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

from engine.domain.ids import WorkspaceId
from engine.ports.source_control import GitResult
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


class GitHubSourceControl:
    """Branches, commits, pushes, and pull requests against GitHub.

    Implements `engine.ports.SourceControl`.
    """

    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        binary_path: str = "gh",
        workspace_provider: WorkspaceProvider | None = None,
        git_binary_path: str = "git",
    ) -> None:
        self._token = token
        self._api_url = api_url
        self._binary_path = binary_path
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
        """Open a pull request with ``gh`` and return its URL."""

        if not branch.strip():
            raise ValueError("branch must not be empty")
        if not title.strip():
            raise ValueError("title must not be empty")
        _refuse_internal_branch(branch)
        root_path = await self._root_path(workspace_id)
        url = await self._gh(
            "pr",
            "create",
            "--head",
            branch,
            # A workflow's base is written as a ref somebody can resolve
            # locally, and `origin/main` is the usual spelling. GitHub wants
            # the branch that ref names, so the remote comes off here rather
            # than every caller having to remember two spellings of one base.
            "--base",
            _base_branch(base_ref),
            "--title",
            title,
            "--body",
            body,
            cwd=root_path,
        )
        if not url:
            raise GitHubSourceControlError("gh returned no pull-request URL")
        return url.splitlines()[-1].strip()

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

    async def _root_path(self, workspace_id: WorkspaceId) -> str:
        if self._workspace_provider is None:
            raise GitHubSourceControlError(
                "this source control was composed without a workspace provider, "
                "so it cannot find the checkout to work in"
            )
        return await self._workspace_provider.root_path(workspace_id)

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

    async def _gh(self, *arguments: str, cwd: str | None = None) -> str:
        return await _run_gh(self._binary_path, arguments, self._environment(), cwd)

    def _environment(self) -> Mapping[str, str] | None:
        if not self._token:
            return None
        return {**os.environ, "GH_TOKEN": self._token}

    def _git_environment(self) -> Mapping[str, str]:
        """The host environment without forge bearer tokens.

        Git authentication belongs to the configured credential helper. A git
        subprocess does not need the token used by `gh`, and Git can invoke
        helpers, hooks and aliases, so putting that token in its environment
        turns any such program into a credential reader.
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


class GitHubSourceControlError(RuntimeError):
    """The GitHub CLI could not perform a source-control operation."""


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


async def _run_gh(
    binary_path: str,
    arguments: tuple[str, ...],
    environment: Mapping[str, str] | None,
    cwd: str | None = None,
) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            binary_path,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=cwd,
        )
    except OSError as error:
        raise GitHubSourceControlError(f"could not start {binary_path}: {error}") from error
    stdout, stderr = await process.communicate()
    if process.returncode:
        detail = stderr.decode(errors="replace").strip() or "unknown error"
        raise GitHubSourceControlError(f"gh failed: {detail}")
    return stdout.decode(errors="replace").strip()


__all__ = [
    "GitGlobalOptionError",
    "GitHubSourceControl",
    "GitHubSourceControlError",
    "GitOutsideWorkspaceError",
    "INTERNAL_BRANCH_PREFIX",
    "InternalBranchPublicationError",
    "UnsafePushSpecificationError",
]

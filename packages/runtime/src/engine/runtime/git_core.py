"""Shared local git execution and publication guards for forge adapters."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence

from engine.ports.source_control import GitResult
from engine.runtime.push_policy import push_spec

DEFAULT_GIT_BINARY = "git"

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


class GitSourceControlError(RuntimeError):
    """A local git operation failed."""


class InternalBranchPublicationError(GitSourceControlError):
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
        GitSourceControlError.__init__(
            self,
            f"push target {refspec!r} is not explicit enough to prove that it "
            "excludes Engine's internal branch; name a concrete destination "
            "such as agent/<description> or HEAD:agent/<description>",
        )
        self.branch = refspec


class GitOutsideWorkspaceError(GitSourceControlError):
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
        GitSourceControlError.__init__(
            self,
            f"git global option {option} is not available through git_subcommand; "
            "pass an ordinary git subcommand and its arguments instead",
        )
        self.option = option


def refuse_internal_branch(branch: str) -> None:
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


def guard_push(
    arguments: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, tuple[str, ...], bool] | None]:
    """Validate public git arguments and return any push ownership requirements."""
    arguments = tuple(str(argument) for argument in arguments)
    if not arguments:
        raise ValueError("git needs at least one argument")
    subcommand = _subcommand_index(arguments)
    if subcommand is None:
        return arguments, None
    if arguments[subcommand] in {"send-pack", "http-push"}:
        raise ValueError("use git push so branch ownership can be checked")
    if arguments[subcommand] != "push":
        return arguments, None
    for destination in _push_destinations(arguments[subcommand:]):
        refuse_internal_branch(destination)
    spec = push_spec(arguments[subcommand:])
    # Prevent remote configuration from expanding an explicit push into a mirror.
    return (*arguments[:subcommand + 1], "--no-mirror", *arguments[subcommand + 1:]), spec


class GitInvoker:
    """Run git with forge credentials removed, preserving adapter error types."""

    def __init__(
        self, binary_path: str = DEFAULT_GIT_BINARY,
        error_type: type[RuntimeError] = GitSourceControlError,
    ) -> None:
        self.binary_path = binary_path
        self.error_type = error_type

    async def run(self, root_path: str, arguments: Sequence[str]) -> GitResult:
        try:
            process = await asyncio.create_subprocess_exec(
                self.binary_path,
                "-C",
                root_path,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment(),
            )
        except OSError as error:
            raise self.error_type(
                f"could not start {self.binary_path}: {error}"
            ) from error
        stdout, stderr = await process.communicate()
        return GitResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace").strip(),
            stderr=stderr.decode(errors="replace").strip(),
        )

    async def checked(self, root_path: str, arguments: Sequence[str]) -> str:
        result = await self.run(root_path, arguments)
        if not result.ok:
            detail = result.stderr or result.stdout or "unknown error"
            raise self.error_type(
                f"git {arguments[0]} failed: {detail}"
            )
        return result.stdout

    def environment(self) -> Mapping[str, str]:
        """The host environment without forge bearer tokens.

        Git authentication belongs to the configured credential helper. A git
        subprocess does not need the tokens used for forge APIs, and git can
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
                "GITLAB_TOKEN",
                "GL_TOKEN",
                "GITLAB_ACCESS_TOKEN",
            }
        }

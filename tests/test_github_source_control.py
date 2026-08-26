"""GitHub source control: git in the workspace, ``gh`` for the rest."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from engine.adapters.source_control.github import (
    GitHubSourceControl,
    GitOutsideWorkspaceError,
    InternalBranchPublicationError,
)
from engine.domain.ids import WorkspaceId


WORKSPACE = WorkspaceId("ws-under-test")
_IDENTITY = ("-c", "user.name=Engine Tests", "-c", "user.email=engine@example.test")


class _OneWorkspace:
    """Enough of `WorkspaceProvider` to resolve a single checkout."""

    def __init__(self, root_path: Path) -> None:
        self._root_path = str(root_path)

    async def root_path(self, workspace_id: WorkspaceId) -> str:
        assert workspace_id == WORKSPACE
        return self._root_path


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _checkout(path: Path, branch: str = "main") -> GitHubSourceControl:
    """A real repository on `branch`, and a source control pointed at it."""
    path.mkdir()
    _git(path, "init", "-b", branch)
    (path / "README.md").write_text("engine\n")
    _git(path, "add", "README.md")
    _git(path, *_IDENTITY, "commit", "-m", "initial")
    # A remote that exists but is never reachable: the guards under test have
    # to refuse before anything is dialled, so a push that gets past one fails
    # loudly rather than quietly talking to something.
    _git(path, "remote", "add", "origin", str(path / "nowhere.git"))
    return GitHubSourceControl("", workspace_provider=_OneWorkspace(path))


# --- git runs in the workspace, and any subcommand is reachable -------------


def test_any_subcommand_runs_in_the_workspace(tmp_path: Path) -> None:
    """The point of the passthrough: no menu, and one bounded directory."""
    source_control = _checkout(tmp_path / "checkout")

    branches = asyncio.run(
        source_control.run_git(WORKSPACE, ["rev-parse", "--abbrev-ref", "HEAD"])
    )
    # A subcommand no named port method would ever have thought to expose.
    searched = asyncio.run(
        source_control.run_git(WORKSPACE, ["log", "--format=%s", "-S", "engine"])
    )

    assert branches.ok
    assert branches.stdout == "main"
    assert searched.stdout == "initial"


def test_a_multi_line_commit_message_survives_being_an_argument(
    tmp_path: Path,
) -> None:
    """An argument vector, not a command line: nothing is split or quoted."""
    checkout = tmp_path / "checkout"
    source_control = _checkout(checkout)
    (checkout / "greeting.txt").write_text("hello\n")
    message = "feat: add a greeting\n\nWith a body that has its own lines."

    asyncio.run(source_control.run_git(WORKSPACE, ["add", "greeting.txt"]))
    committed = asyncio.run(
        source_control.run_git(
            WORKSPACE, [*_IDENTITY, "commit", "--message", message]
        )
    )

    assert committed.ok, committed.stderr
    assert _git(checkout, "log", "-1", "--format=%B").strip() == message


def test_a_failing_command_is_reported_rather_than_raised(tmp_path: Path) -> None:
    """Half of git answers questions with its exit code.

    `diff --exit-code` says "there are changes" that way, so raising on every
    non-zero exit would make a whole class of git unusable through the tool.
    """
    checkout = tmp_path / "checkout"
    source_control = _checkout(checkout)
    (checkout / "README.md").write_text("changed\n")

    result = asyncio.run(source_control.run_git(WORKSPACE, ["diff", "--exit-code"]))
    unknown = asyncio.run(source_control.run_git(WORKSPACE, ["frobnicate"]))

    assert not result.ok
    assert result.exit_code == 1
    assert "README.md" in result.stdout
    assert not unknown.ok
    assert "frobnicate" in unknown.stderr


def test_git_needs_at_least_one_argument(tmp_path: Path) -> None:
    source_control = _checkout(tmp_path / "checkout")

    with pytest.raises(ValueError):
        asyncio.run(source_control.run_git(WORKSPACE, []))


# --- and the one thing it will not do ---------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        ["push", "--set-upstream", "origin", "engine/ws-under-test"],
        ["push", "origin", "HEAD:refs/heads/engine/ws-under-test"],
        ["push", "origin", "+engine/ws-under-test:engine/ws-under-test"],
        # Global options ahead of the subcommand are still a push.
        ["-c", "push.default=current", "push", "origin", "engine/ws-under-test"],
    ],
)
def test_an_internal_branch_is_never_published(
    tmp_path: Path, arguments: list[str]
) -> None:
    """`engine/<workspace>` is Engine's bookkeeping, not a proposed change.

    Enforced here rather than asked for in a prompt, which is the difference
    between a rule and a suggestion.
    """
    source_control = _checkout(tmp_path / "checkout")

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(source_control.run_git(WORKSPACE, arguments))


def test_a_push_naming_no_refspec_is_judged_by_the_checked_out_branch(
    tmp_path: Path,
) -> None:
    """The refusable case with nothing in argv to refuse.

    `git push` on the workspace's own branch is the shortest route to leaking
    it, and the argument vector says nothing at all about which branch that is.
    """
    source_control = _checkout(tmp_path / "checkout", branch="engine/ws-under-test")

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(source_control.run_git(WORKSPACE, ["push", "origin"]))


def test_a_descriptive_branch_is_not_refused(tmp_path: Path) -> None:
    """The guard is about one prefix, and must not read as "no pushing"."""
    checkout = tmp_path / "checkout"
    source_control = _checkout(checkout)
    _git(checkout, "branch", "agent/add-a-greeting")

    # Reaching git at all is the assertion: the push then fails on the remote
    # that was never meant to answer, which is a report rather than a refusal.
    result = asyncio.run(
        source_control.run_git(WORKSPACE, ["push", "origin", "agent/add-a-greeting"])
    )

    assert not result.ok
    assert "nowhere.git" in f"{result.stderr}\n{result.stdout}"


@pytest.mark.parametrize(
    "arguments",
    [
        ["-C", "/etc", "status"],
        ["--git-dir=/elsewhere/.git", "log"],
        ["--work-tree", "/elsewhere", "status"],
    ],
)
def test_git_cannot_be_pointed_at_another_repository(
    tmp_path: Path, arguments: list[str]
) -> None:
    """The command line is the agent's; the directory is not.

    `git -C a -C b` composes rather than replaces, so a second `-C` is how an
    unbounded argument vector would quietly become an unbounded tool.
    """
    source_control = _checkout(tmp_path / "checkout")

    with pytest.raises(GitOutsideWorkspaceError):
        asyncio.run(source_control.run_git(WORKSPACE, arguments))


def test_configuration_overrides_are_still_the_agent_s_to_pass(
    tmp_path: Path,
) -> None:
    """`-c` is not `-C`: it configures the command, it does not relocate it."""
    source_control = _checkout(tmp_path / "checkout")

    result = asyncio.run(
        source_control.run_git(WORKSPACE, ["-c", "core.abbrev=12", "rev-parse", "HEAD"])
    )

    assert result.ok, result.stderr


def test_git_without_a_workspace_provider_says_so() -> None:
    with pytest.raises(RuntimeError, match="workspace provider"):
        asyncio.run(GitHubSourceControl("").run_git(WORKSPACE, ["status"]))


# --- opening the review ------------------------------------------------------


def test_opening_a_review_proposes_against_the_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow's base is written `origin/main`; GitHub wants `main`."""
    source_control = _checkout(tmp_path / "checkout")
    calls: list[tuple[str, ...]] = []

    async def gh(*arguments: str, cwd: str | None = None) -> str:
        calls.append(arguments)
        return "https://github.com/acme/api/pull/42"

    monkeypatch.setattr(source_control, "_gh", gh)

    url = asyncio.run(
        source_control.request_review(
            WORKSPACE, "agent/add-a-greeting", "origin/main", "feat: greet", "Body."
        )
    )

    assert url == "https://github.com/acme/api/pull/42"
    assert calls == [
        (
            "pr",
            "create",
            "--head",
            "agent/add-a-greeting",
            "--base",
            "main",
            "--title",
            "feat: greet",
            "--body",
            "Body.",
        )
    ]


def test_a_base_branch_with_a_slash_in_it_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`release/2.0` is a branch name, not a remote and a branch."""
    source_control = _checkout(tmp_path / "checkout")
    calls: list[tuple[str, ...]] = []

    async def gh(*arguments: str, cwd: str | None = None) -> str:
        calls.append(arguments)
        return "https://github.com/acme/api/pull/42"

    monkeypatch.setattr(source_control, "_gh", gh)

    asyncio.run(
        source_control.request_review(
            WORKSPACE, "agent/fix", "release/2.0", "fix: it", ""
        )
    )

    assert calls[0][calls[0].index("--base") + 1] == "release/2.0"


def test_a_review_is_never_opened_for_an_internal_branch(tmp_path: Path) -> None:
    source_control = _checkout(tmp_path / "checkout")

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(
            source_control.request_review(
                WORKSPACE, "engine/ws-under-test", "main", "feat: leak", ""
            )
        )


# --- comments ----------------------------------------------------------------


def test_general_comment_uses_gh_pr_comment(monkeypatch: pytest.MonkeyPatch) -> None:
    source_control = GitHubSourceControl("")
    calls: list[tuple[str, ...]] = []

    async def gh(*arguments: str) -> str:
        calls.append(arguments)
        return ""

    monkeypatch.setattr(source_control, "_gh", gh)

    asyncio.run(
        source_control.add_comment(
            "https://github.com/acme/api/pull/42", "Looks good."
        )
    )

    assert calls == [
        (
            "pr",
            "comment",
            "https://github.com/acme/api/pull/42",
            "--body",
            "Looks good.",
        )
    ]


def test_inline_comment_resolves_head_and_posts_review_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_control = GitHubSourceControl("")
    calls: list[tuple[str, ...]] = []

    async def gh(*arguments: str) -> str:
        calls.append(arguments)
        return "abc123" if arguments[:2] == ("pr", "view") else ""

    monkeypatch.setattr(source_control, "_gh", gh)

    asyncio.run(
        source_control.add_comment(
            "https://github.com/acme/api/pull/42",
            "This can race.",
            "src/worker.py",
            17,
        )
    )

    assert calls[0] == (
        "pr",
        "view",
        "https://github.com/acme/api/pull/42",
        "--json",
        "headRefOid",
        "--jq",
        ".headRefOid",
    )
    assert calls[1] == (
        "api",
        "--method",
        "POST",
        "repos/acme/api/pulls/42/comments",
        "--raw-field",
        "body=This can race.",
        "--raw-field",
        "commit_id=abc123",
        "--raw-field",
        "path=src/worker.py",
        "--field",
        "line=17",
        "--raw-field",
        "side=RIGHT",
    )


@pytest.mark.parametrize(
    ("file", "line"), [("src/worker.py", None), (None, 17), ("src/worker.py", 0)]
)
def test_inline_comment_requires_a_valid_file_and_line(
    file: str | None, line: int | None
) -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            GitHubSourceControl("").add_comment(
                "https://github.com/acme/api/pull/42", "Finding.", file, line
            )
        )

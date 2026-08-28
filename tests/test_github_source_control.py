"""GitHub source control: git in the workspace, ``gh`` for the rest."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from engine.adapters.source_control.github import (
    GitGlobalOptionError,
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
    _git(path, "config", "user.name", "Engine Tests")
    _git(path, "config", "user.email", "engine@example.test")
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
        source_control.run_git(WORKSPACE, ["commit", "--message", message])
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


def test_a_push_naming_no_refspec_is_refused(
    tmp_path: Path,
) -> None:
    """The refusable case with nothing in argv to refuse.

    `git push` says nothing about which source or destination its configuration
    will choose, so there is no branch name in argv for the guard to validate.
    """
    source_control = _checkout(tmp_path / "checkout", branch="engine/ws-under-test")

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(source_control.run_git(WORKSPACE, ["push", "origin"]))


@pytest.mark.parametrize(
    "arguments",
    [
        ["push", "origin", "HEAD"],
        ["push", "origin", "@"],
        ["push", "--all", "origin"],
        ["push", "--branches", "origin"],
        ["push", "--mirror", "origin"],
        ["push", "origin", ":"],
        ["push", "origin", "refs/heads/*:refs/heads/*"],
    ],
)
def test_ambiguous_and_bulk_pushes_are_refused(
    tmp_path: Path, arguments: list[str]
) -> None:
    """Every allowed branch push identifies its remote destination in argv."""
    source_control = _checkout(tmp_path / "checkout", branch="engine/ws-under-test")

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(source_control.run_git(WORKSPACE, arguments))


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


def test_head_is_safe_when_its_destination_is_explicit(tmp_path: Path) -> None:
    source_control = _checkout(tmp_path / "checkout")

    result = asyncio.run(
        source_control.run_git(
            WORKSPACE, ["push", "origin", "HEAD:refs/heads/agent/add-a-greeting"]
        )
    )

    assert not result.ok
    assert "nowhere.git" in f"{result.stderr}\n{result.stdout}"


@pytest.mark.parametrize(
    "arguments",
    [
        ["-C", "/etc", "status"],
        ["--git-dir=/elsewhere/.git", "log"],
        ["--work-tree", "/elsewhere", "status"],
        ["-c", "alias.x=!sh", "x"],
        ["--exec-path=/tmp", "x"],
        ["--config-env", "alias.x=PAYLOAD", "x"],
    ],
)
def test_git_global_options_cannot_select_config_or_executables(
    tmp_path: Path, arguments: list[str]
) -> None:
    """Global git options can be process launchers in disguise.

    `-c alias.x=!sh` and `--exec-path` are direct code-execution paths. An
    allowlist also covers the next global option git adds without requiring a
    security reviewer to hear about it first.
    """
    source_control = _checkout(tmp_path / "checkout")

    with pytest.raises((GitGlobalOptionError, GitOutsideWorkspaceError)):
        asyncio.run(source_control.run_git(WORKSPACE, arguments))


def test_value_free_safe_global_options_still_pass(tmp_path: Path) -> None:
    source_control = _checkout(tmp_path / "checkout")

    result = asyncio.run(
        source_control.run_git(WORKSPACE, ["--no-pager", "rev-parse", "HEAD"])
    )

    assert result.ok, result.stderr


def test_git_never_receives_the_forge_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_control = GitHubSourceControl("adapter-secret", workspace_provider=_OneWorkspace(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "host-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "other-host-secret")

    environment = source_control._git_environment()

    assert "GH_TOKEN" not in environment
    assert "GITHUB_TOKEN" not in environment


def test_git_without_a_workspace_provider_says_so() -> None:
    with pytest.raises(RuntimeError, match="workspace provider"):
        asyncio.run(GitHubSourceControl("").run_git(WORKSPACE, ["status"]))


# --- opening the review ------------------------------------------------------


def test_opening_a_review_proposes_against_the_base_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow's base is written `origin/main`; GitHub wants `main`."""
    checkout = tmp_path / "checkout"
    source_control = _checkout(checkout)
    api_calls: list[tuple[str, str, dict]] = []

    async def fake_api(self_inner, method: str, path: str, **kwargs: object) -> dict:
        api_calls.append((method, path, kwargs.get("json", {})))
        return {"html_url": "https://github.com/acme/api/pull/42"}

    monkeypatch.setattr(type(source_control), "_api", fake_api)

    url = asyncio.run(
        source_control.request_review(
            WORKSPACE, "agent/add-a-greeting", "origin/main", "feat: greet", "Body."
        )
    )

    assert url == "https://github.com/acme/api/pull/42"
    assert len(api_calls) == 1
    method, path, payload = api_calls[0]
    assert method == "POST"
    assert path.endswith("/pulls")
    assert payload["head"] == "agent/add-a-greeting"
    assert payload["base"] == "main"
    assert payload["title"] == "feat: greet"
    assert payload["body"] == "Body."


def test_a_base_branch_with_a_slash_in_it_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`release/2.0` is a branch name, not a remote and a branch."""
    source_control = _checkout(tmp_path / "checkout")
    api_calls: list[tuple[str, str, dict]] = []

    async def fake_api(self_inner, method: str, path: str, **kwargs: object) -> dict:
        api_calls.append((method, path, kwargs.get("json", {})))
        return {"html_url": "https://github.com/acme/api/pull/42"}

    monkeypatch.setattr(type(source_control), "_api", fake_api)

    asyncio.run(
        source_control.request_review(
            WORKSPACE, "agent/fix", "release/2.0", "fix: it", ""
        )
    )

    assert api_calls[0][2]["base"] == "release/2.0"


def test_a_review_is_never_opened_for_an_internal_branch(tmp_path: Path) -> None:
    source_control = _checkout(tmp_path / "checkout")

    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(
            source_control.request_review(
                WORKSPACE, "engine/ws-under-test", "main", "feat: leak", ""
            )
        )


# --- comments ----------------------------------------------------------------


def test_general_comment_posts_to_the_issues_comments_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_control = GitHubSourceControl("")
    api_calls: list[tuple[str, str, dict]] = []

    async def fake_api(self_inner, method: str, path: str, **kwargs: object) -> dict:
        api_calls.append((method, path, kwargs.get("json", {})))
        return {}

    monkeypatch.setattr(type(source_control), "_api", fake_api)

    asyncio.run(
        source_control.add_comment(
            "https://github.com/acme/api/pull/42", "Looks good."
        )
    )

    assert len(api_calls) == 1
    method, path, payload = api_calls[0]
    assert method == "POST"
    assert path == "/repos/acme/api/issues/42/comments"
    assert payload == {"body": "Looks good."}


def test_inline_comment_resolves_head_and_posts_review_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_control = GitHubSourceControl("")
    api_calls: list[tuple[str, str, dict]] = []

    async def fake_api(self_inner, method: str, path: str, **kwargs: object) -> dict:
        api_calls.append((method, path, kwargs.get("json", {})))
        if method == "GET" and path.endswith("/pulls/42"):
            return {"head": {"sha": "abc123"}}
        return {}

    monkeypatch.setattr(type(source_control), "_api", fake_api)

    asyncio.run(
        source_control.add_comment(
            "https://github.com/acme/api/pull/42",
            "This can race.",
            "src/worker.py",
            17,
        )
    )

    assert len(api_calls) == 2
    # First call fetches the PR to get the head SHA.
    get_method, get_path, _ = api_calls[0]
    assert get_method == "GET"
    assert get_path == "/repos/acme/api/pulls/42"
    # Second call posts the inline review comment.
    post_method, post_path, post_payload = api_calls[1]
    assert post_method == "POST"
    assert post_path == "/repos/acme/api/pulls/42/comments"
    assert post_payload["body"] == "This can race."
    assert post_payload["commit_id"] == "abc123"
    assert post_payload["path"] == "src/worker.py"
    assert post_payload["line"] == 17
    assert post_payload["side"] == "RIGHT"


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

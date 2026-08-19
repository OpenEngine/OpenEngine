"""GitHub source-control comments use the local ``gh`` CLI safely."""

import asyncio

import pytest

from engine.adapters.source_control.github import GitHubSourceControl


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

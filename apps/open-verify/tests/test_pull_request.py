import asyncio
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from open_verify.changes import read_change
from open_verify.github import PullRequest
from open_verify.pull_request import PullRequestWorkspace, copy_setup, run_pull_request, setup_files

HEAD, BASE = "a" * 40, "b" * 40
TARGET = PullRequest("owner/repo", 1, HEAD, BASE, BASE)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/o/r/pull/1",
        "https://github.com.evil/o/r/pull/1",
        "https://github.com/o/../pull/1",
        "https://user@github.com/o/r/pull/1",
    ],
)
def test_rejects_invalid_pr_urls(url):
    with pytest.raises(ValueError):
        PullRequest.parse(url)


def test_setup_is_explicit_private_and_cannot_overwrite_pr_files(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / ".env").write_text("SECRET=private")
    assert setup_files(source, []) == []
    files = setup_files(source, [".env"])
    copy_setup(files, target)
    assert (target / ".env").read_text() == "SECRET=private"
    assert (target / ".env").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError):
        copy_setup(files, target)
    with pytest.raises(ValueError):
        setup_files(source, ["../source/.env"])


@pytest.mark.parametrize("failure", [None, "test", "prepare", "cancel", "publish", "cleanup"])
def test_orchestration_keeps_artifacts_and_cleans_before_publishing(tmp_path, failure):
    calls = []
    root = tmp_path / "isolated"
    root.mkdir()
    project = root / "checkout"
    project.mkdir()

    class Workspace:
        def __init__(self, target):
            self.root, self.path = root, project

        async def prepare(self):
            calls.append("prepare")
            if failure == "prepare":
                raise ValueError("Fetch failed")
            return self.path

        async def close(self):
            calls.append("cleanup")
            if failure == "cleanup":
                raise RuntimeError("Cleanup failed")

    async def verify(args, *, artifacts, prepare):
        calls.append("test")
        assert args.project == project
        assert args.base == BASE and args.head == HEAD
        assert not args.include_working_tree
        prepare()
        if failure == "test":
            raise ValueError("Runtime failed")
        if failure == "cancel":
            raise asyncio.CancelledError()
        artifacts.write("manifest.json", {"fixture": True})
        return 0

    async def publish(*args):
        calls.append("publish")
        assert calls[-2] == "cleanup"
        if failure == "publish":
            raise ValueError("PR changed")
        return TARGET.url + "#issuecomment-1"

    github = SimpleNamespace(
        resolve=AsyncMock(return_value=TARGET), ensure_current=AsyncMock(), publish=publish
    )
    args = SimpleNamespace(
        project=tmp_path,
        pr=TARGET.url,
        output=tmp_path / "runs",
        setup_file=[],
        publish=True,
        request="Check login",
    )
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run_pull_request(args, verify, github=github, workspace_factory=Workspace))
    else:
        assert asyncio.run(
            run_pull_request(args, verify, github=github, workspace_factory=Workspace)
        ) == (0 if failure is None else 2)
    assert "cleanup" in calls
    if failure in {"test", "prepare", "cancel", "cleanup"}:
        assert "publish" not in calls
    assert args.project == tmp_path  # Caller remains on its original checkout.
    if failure != "cancel":
        assert list((tmp_path / "runs").glob("*/manifest.json"))


def test_actual_worktree_tests_pr_head_and_preserves_dirty_source_checkout(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return (
            subprocess.check_output(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.com",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *args,
                ],
                cwd=source,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )

    git("init")
    (source / "app.txt").write_text("before")
    git("add", "app.txt")
    git("commit", "-m", "test: base")
    base = git("rev-parse", "HEAD")
    (source / "app.txt").write_text("PR behavior")
    git("commit", "-am", "test: PR change")
    head = git("rev-parse", "HEAD")
    git("update-ref", "refs/pull/1/head", head)
    (source / "ov-only.txt").write_text("implementation branch")
    git("add", "ov-only.txt")
    git("commit", "-m", "test: local ov implementation")
    original_head = git("rev-parse", "HEAD")
    (source / "app.txt").write_text("uncommitted user work")
    original_status = git("status", "--porcelain")

    class LocalWorkspace(PullRequestWorkspace):
        async def git(self, *args):
            # All real Git work is confined to disposable fixtures; no network.
            args = tuple(
                str(source) if a == "https://github.com/owner/repo.git" else a for a in args
            )
            return await super().git(*args)

    async def run():
        workspace = LocalWorkspace(PullRequest("owner/repo", 1, head, base, base))
        try:
            path = await workspace.prepare()
            assert (path / "app.txt").read_text() == "PR behavior"
            assert not (path / "ov-only.txt").exists()
            change = await read_change(path, base, head)
            assert change.files == ["app.txt"] and "+PR behavior" in change.diff
        finally:
            await workspace.close()
        assert not workspace.root.exists()

    asyncio.run(run())
    assert git("rev-parse", "HEAD") == original_head
    assert git("status", "--porcelain") == original_status
    assert (source / "app.txt").read_text() == "uncommitted user work"

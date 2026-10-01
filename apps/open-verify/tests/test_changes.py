import asyncio

import pytest

from open_verify.changes import MAX_DIFF, Change, DiffRequest, read_change, read_file_diff


class GitFixture:
    def __init__(
        self, *, head="head-sha", dirty="", files="app.py\0", diff="+new behavior", numstat=""
    ):
        self.head, self.dirty, self.files, self.diff = head, dirty, files, diff
        self.numstat = numstat
        self.calls = []

    async def read(self, *args):
        self.calls.append(args)
        if args[0] == "rev-parse":
            return {
                "main^{commit}": "base-sha",
                "feature^{commit}": self.head,
                "HEAD^{commit}": "head-sha",
            }[args[-1]]
        if args[0] == "status":
            return self.dirty
        if args[0] == "ls-files":
            return "new.py\0"
        if "--name-only" in args:
            return self.files
        if "--numstat" in args:
            return self.numstat
        return self.diff


def test_resolves_revisions_and_uses_literal_paths_without_mutation(tmp_path):
    git = GitFixture(files=":(glob)*.py\0")
    change = asyncio.run(read_change(tmp_path, "main", "feature", reader=git))
    assert (change.base, change.head) == ("base-sha", "head-sha")
    assert change.files == [":(glob)*.py"]
    assert git.calls[-1][-1] == ":(literal):(glob)*.py"
    assert all(call[0] in {"rev-parse", "status", "diff"} for call in git.calls)


@pytest.mark.parametrize(
    "git, message",
    [
        (GitFixture(head="other"), "must match"),
        (GitFixture(dirty=" M app.py\0"), "local changes"),
    ],
)
def test_rejects_mismatched_or_dirty_checkout(tmp_path, git, message):
    with pytest.raises(ValueError, match=message):
        asyncio.run(read_change(tmp_path, "main", "feature", reader=git))


def test_working_tree_is_explicit_and_includes_untracked_files(tmp_path):
    (tmp_path / "new.py").write_text("print('new')", encoding="utf-8")
    git = GitFixture(dirty="?? new.py\0")
    change = asyncio.run(
        read_change(
            tmp_path,
            "main",
            reader=git,
            include_working_tree=True,
        )
    )
    assert change.include_working_tree
    assert change.files == ["app.py", "new.py"]
    assert "print('new')" in change.diff
    assert "head-sha" not in git.calls[-1]  # tracked diff is against the worktree


def test_secrets_and_dependency_directories_are_excluded_and_large_diff_is_marked(tmp_path):
    git = GitFixture(files=".env\0cert.key\0node_modules/a.js\0app.py\0", diff="x" * (MAX_DIFF + 1))
    change = asyncio.run(read_change(tmp_path, "main", reader=git))
    assert change.files == ["app.py"]
    assert set(change.excluded_files) == {".env", "cert.key", "node_modules/a.js"}
    assert change.truncated and len(change.diff) == MAX_DIFF
    assert len(change.fingerprint) == 64
    assert not any(call[-1] == ":(literal).env" for call in git.calls)


@pytest.mark.parametrize(
    "endpoints",
    [
        "a/logo.png and b/logo.png",
        "/dev/null and b/logo.png",
        "a/logo.png and /dev/null",
    ],
)
def test_tracked_binary_content_is_marked_incomplete(tmp_path, endpoints):
    git = GitFixture(
        files="logo.png\0",
        numstat="-\t-\tlogo.png\0",
        diff=f"diff --git a/logo.png b/logo.png\nBinary files {endpoints} differ\n",
    )
    change = asyncio.run(read_change(tmp_path, "main", reader=git))
    assert change.files == ["logo.png"]
    assert change.truncated
    assert "Binary files" in change.diff


def test_binary_notice_in_text_content_does_not_mark_inspection_incomplete(tmp_path):
    git = GitFixture(
        numstat="1\t0\tapp.py\0", diff="+Binary files a/logo.png and b/logo.png differ\n"
    )
    change = asyncio.run(read_change(tmp_path, "main", reader=git))
    assert not change.truncated


def test_can_page_relevant_diff_beyond_initial_preview(tmp_path):
    git = GitFixture(diff='x' * MAX_DIFF + '\n+important login change')
    change = Change(base='base-sha', head='head-sha', files=['app.py'], truncated=True)
    result = asyncio.run(read_file_diff(tmp_path, change,
        DiffRequest(path='app.py', offset=MAX_DIFF, limit=200), reader=git))
    assert '+important login change' in result['diff']
    assert result['next_offset'] is None
    assert git.calls[-1][-1] == ':(literal)app.py'


@pytest.mark.parametrize('path', ['.env', '../secret.py', '/tmp/secret.py', 'other.py'])
def test_diff_reader_rejects_paths_outside_change(tmp_path, path):
    change = Change(base='base-sha', head='head-sha', files=['app.py'])
    with pytest.raises(ValueError, match='inspectable'):
        asyncio.run(read_file_diff(tmp_path, change, DiffRequest(path=path), reader=GitFixture()))

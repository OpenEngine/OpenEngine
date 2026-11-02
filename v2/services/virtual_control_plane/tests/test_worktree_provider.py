"""The Git worktree adapter against real temporary repositories and a lock-holding daemon stand-in.

Runs on macOS and Linux with the host's `git`; no VM binary, image, guest
cgroup or broker is involved. Covers revision selection, independence from
the source checkout and unrelated worktrees, crash recovery, stale metadata,
symlink escapes, branch ownership, dirty-worktree cleanup policy and confirmed
daemon termination.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from openengine.messaging import ServiceError, envelopes

from virtual_control_plane.application.ports import Ownership, OwnershipMismatch
from virtual_control_plane.providers.worktree import HostDaemon, Worktree, lock_reason
from virtual_control_plane.providers.worktree import daemon as daemon_module
from virtual_control_plane.providers.worktree.daemon import INHERITED_VARIABLES
from virtual_control_plane.tests import worktree_daemon_fake

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="worktree workspaces run on macOS and Linux")

OWNERSHIP = Ownership("ws_a", "rt_a", 1)
FAKE_DAEMON = (sys.executable, "-I", worktree_daemon_fake.__file__)


def git(path: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.test", "-C", str(path), *arguments],
                          check=True, capture_output=True, text=True).stdout


def document(**spec) -> dict:
    return {"apiVersion": "openengine.cc/v1alpha1", "kind": "Workspace", "metadata": {"name": "example"},
            "spec": {"provider": "process", "tools": ["git", "terminal"], **spec}}


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """The caller's primary checkout: `main` checked out with a local edit, a `feature` branch, an unrelated worktree."""
    path = tmp_path / "origin"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "README").write_text("main\n")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "main")
    git(path, "checkout", "-q", "-b", "feature")
    (path / "README").write_text("feature\n")
    git(path, "commit", "-q", "-am", "feature")
    git(path, "checkout", "-q", "main")
    git(path, "worktree", "add", "-q", str(tmp_path / "unrelated"), "feature")
    (path / "README").write_text("caller's uncommitted edit\n")
    return path


def evidence(origin: Path, tmp_path: Path) -> dict:
    """Everything about the caller's repository VCP must leave unchanged."""
    return {"status": git(origin, "status", "--porcelain"), "head": git(origin, "rev-parse", "HEAD"),
            "branch": git(origin, "branch", "--show-current"), "readme": (origin / "README").read_text(),
            "refs": git(origin, "for-each-ref", "--format=%(refname) %(objectname)"),
            "unrelated": (tmp_path / "unrelated" / "README").read_text(),
            "unrelated_head": git(tmp_path / "unrelated", "rev-parse", "HEAD"),
            "worktrees": [block for block in git(origin, "worktree", "list", "--porcelain").split("\n\n")
                          if "/owned/" not in block]}


def registered(origin: Path, path: Path) -> list[str]:
    return [line for line in git(origin, "worktree", "list", "--porcelain").splitlines() if line == f"worktree {path}"]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve() / "owned"


def adapter(root: Path, **options) -> Worktree:
    """Repositories are allowed beneath the test's temporary directory, which also holds `root`."""
    return Worktree(root, tools=("git", "terminal", "oe-missing-tool"), source_roots=(root.parent,), **options)


def spec(origin: Path, **context) -> dict:
    return document(context={"repositories": [{"url": str(origin), "ref": "feature"}], **context})


def run(coroutine):
    return asyncio.run(coroutine)


def worktree_path(root: Path, ownership: Ownership = OWNERSHIP, index: int = 0) -> Path:
    return root / ownership.runtime_id / "workspace" / "repositories" / str(index)


def test_capabilities_are_process_level_and_validation_rejects_what_a_host_cannot_honor(root: Path) -> None:
    worktree = Worktree(root, tools=("terminal", "git", "terminal"), source_roots=("/srv",))

    capabilities = worktree.capabilities

    envelopes.check("virtual_control_plane/v1/schemas/capabilities.schema.json#/$defs/provider_capabilities", capabilities)
    assert capabilities["isolation"] == "process" and capabilities["tools"] == ["terminal", "git"]
    assert capabilities["max_resources"] == {} and not capabilities["checkpoint"] and capabilities["restore"] == "none"
    worktree.validate(document(context={"repositories": [{"url": "file:///srv/repo"}, {"url": "/srv/other", "ref": "v1"}],
                                        "files": [{"path": "notes/a.md", "content": ""}]}))
    with pytest.raises(ServiceError) as error:
        worktree.validate(document(resources={"cpuMillis": 1000}, context={
            "repositories": [{"url": "https://example.test/repo.git"}, {"url": "relative"}, {"url": "/srv/r", "ref": "--upload-pack=x"},
                             {"url": "file:///home/someone/secrets"}, {"url": "/srv/../home/someone/secrets"}],
            "files": [{"path": "../escape", "content": ""}, {"path": "/abs", "content": ""}, {"path": "a/./b", "content": ""},
                      {"path": "repositories/0/.git", "content": ""}]}))
    assert [violation["field"] for violation in error.value.details["violations"]] == [
        "payload.workspace.spec.resources",
        "payload.workspace.spec.context.repositories[0].url",
        "payload.workspace.spec.context.repositories[1].url",
        "payload.workspace.spec.context.repositories[2].ref",
        "payload.workspace.spec.context.repositories[3].url",
        "payload.workspace.spec.context.repositories[4].url",
        "payload.workspace.spec.context.files[0].path",
        "payload.workspace.spec.context.files[1].path",
        "payload.workspace.spec.context.files[2].path",
        "payload.workspace.spec.context.files[3].path",
    ]
    # Without repositories nothing is a worktree, so `repositories/` is an ordinary context path.
    worktree.validate(document(context={"files": [{"path": "repositories/notes.md", "content": ""}]}))
    # With no allowed source roots configured, no repository can be named at all.
    with pytest.raises(ServiceError):
        Worktree(root).validate(document(context={"repositories": [{"url": "/srv/repo"}]}))


def test_a_repository_outside_the_allowed_source_roots_is_never_checked_out(origin: Path, root: Path, tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "link").symlink_to(origin)  # a symlink inside an allowed root does not make its target allowed
    worktree = Worktree(root, source_roots=(allowed,))

    for url in (str(origin), str(allowed / "link")):
        with pytest.raises(ServiceError) as error:
            run(worktree.provision(OWNERSHIP, document(context={"repositories": [{"url": url}]})))
        assert error.value.code == "PROVIDER_ERROR" and "source root" in error.value.message
    assert not (root / "rt_a").exists() and registered(origin, worktree_path(root)) == []


def test_source_repository_hooks_and_fsmonitor_never_run(origin: Path, root: Path, tmp_path: Path) -> None:
    ran = tmp_path / "ran"
    hook = origin / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho hook >> {ran}\n")
    hook.chmod(0o755)
    monitor = tmp_path / "fsmonitor"
    monitor.write_text(f"#!/bin/sh\necho fsmonitor >> {ran}\n")
    monitor.chmod(0o755)
    git(origin, "config", "core.fsmonitor", str(monitor))
    worktree = adapter(root)

    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    run(worktree.destroy(OWNERSHIP, locator))

    assert not ran.exists()


def test_provision_creates_an_independent_detached_worktree_and_leaves_the_caller_alone(
    origin: Path, root: Path, tmp_path: Path,
) -> None:
    before = evidence(origin, tmp_path)
    worktree = adapter(root)
    specification = spec(origin, instructions="Be careful.\n", files=[{"path": "notes/todo.md", "content": "x\n"}])

    locator = run(worktree.provision(OWNERSHIP, specification))
    again = run(worktree.provision(OWNERSHIP, specification))
    ensured = run(worktree.ensure(OWNERSHIP, locator, specification))

    path = worktree_path(root)
    assert locator == again == ensured
    assert (path / "README").read_text() == "feature\n"
    assert git(path, "rev-parse", "HEAD") == git(origin, "rev-parse", "feature")
    assert git(path, "branch", "--show-current") == ""  # detached: no branch is created or checked out
    assert git(path, "status", "--porcelain") == ""
    assert registered(origin, path) == [f"worktree {path}"]
    assert f"locked {lock_reason(OWNERSHIP)}" in git(origin, "worktree", "list", "--porcelain")
    workspace = root / "rt_a" / "workspace"
    assert (workspace / "AGENTS.md").read_text() == "Be careful.\n"
    assert (workspace / "notes" / "todo.md").read_text() == "x\n"
    assert not (root / "rt_a" / "state").relative_to(root / "rt_a").is_relative_to("workspace")
    assert (root / "rt_a" / "state").stat().st_mode & 0o777 == 0o700
    context = worktree.context(OWNERSHIP, locator, specification)
    assert (context.root, context.working_directory, context.instruction_files) == (str(workspace), "repositories/0", ("AGENTS.md",))
    owner = json.loads((root / "rt_a" / "owner.json").read_text())
    assert owner["repositories"] == [{"source": str(origin.resolve()), "revision": git(origin, "rev-parse", "feature").strip(),
                                      "path": str(path)}]
    assert evidence(origin, tmp_path) == before

    run(worktree.destroy(OWNERSHIP, locator))
    assert not (root / "rt_a").exists() and registered(origin, path) == []


def test_default_revision_is_the_source_head_and_a_missing_ref_is_terminal(origin: Path, root: Path) -> None:
    worktree = adapter(root)
    run(worktree.provision(OWNERSHIP, document(context={"repositories": [{"url": f"file://{origin}"}]})))
    assert git(worktree_path(root), "rev-parse", "HEAD") == git(origin, "rev-parse", "main")

    other = Ownership("ws_b", "rt_b", 1)
    with pytest.raises(ServiceError) as error:
        run(worktree.provision(other, document(context={"repositories": [{"url": str(origin), "ref": "nope"}]})))
    assert error.value.code == "PROVIDER_ERROR" and not error.value.retryable
    assert not (root / "rt_b").exists()  # nothing claimed for a revision that does not exist


def test_a_crash_after_worktree_creation_resumes_without_a_second_worktree(origin: Path, root: Path, tmp_path: Path) -> None:
    before = evidence(origin, tmp_path)
    worktree = adapter(root)
    materialize = worktree._materialize

    async def crash(*args):
        await materialize(*args)
        raise asyncio.CancelledError()

    worktree._materialize = crash
    with pytest.raises(asyncio.CancelledError):
        run(worktree.provision(OWNERSHIP, spec(origin, instructions="x")))
    assert not (root / "rt_a" / "ready").exists() and registered(origin, worktree_path(root))

    restarted = adapter(root)
    locator = run(restarted.provision(OWNERSHIP, spec(origin, instructions="x")))

    assert registered(origin, worktree_path(root)) == [f"worktree {worktree_path(root)}"]
    assert len([name for name in os.listdir(root) if not name.startswith(".")]) == 1
    assert (root / "rt_a" / "workspace" / "AGENTS.md").read_text() == "x"
    assert locator["repositories"][0]["path"] == str(worktree_path(root))
    assert evidence(origin, tmp_path) == before


def interrupted_after_claim(worktree: Worktree) -> Worktree:
    claim = worktree._claim

    async def claim_only(ownership, specification):
        await claim(ownership, specification)
        raise asyncio.CancelledError()

    worktree._claim = claim_only
    return worktree


def test_partial_creation_is_finished_from_a_stale_registration_or_an_unregistered_directory(origin: Path, root: Path) -> None:
    path = worktree_path(root)
    with pytest.raises(asyncio.CancelledError):
        run(interrupted_after_claim(adapter(root)).provision(OWNERSHIP, spec(origin)))
    # Git registered the worktree, then its directory vanished before preparation finished.
    git(origin, "worktree", "add", "-q", "--lock", "--reason", lock_reason(OWNERSHIP), "--detach", str(path), "main")
    subprocess.run(["rm", "-rf", str(path)], check=True)

    run(adapter(root).provision(OWNERSHIP, spec(origin)))
    assert registered(origin, path) == [f"worktree {path}"]
    assert git(path, "rev-parse", "HEAD") == git(origin, "rev-parse", "feature")

    # A directory Git never registered, left inside our own unprepared runtime, is replaced.
    other = Ownership("ws_b", "rt_b", 1)
    with pytest.raises(asyncio.CancelledError):
        run(interrupted_after_claim(adapter(root)).provision(other, spec(origin)))
    (worktree_path(root, other)).mkdir(parents=True)
    (worktree_path(root, other) / "partial").write_text("half-copied")
    run(adapter(root).provision(other, spec(origin)))
    assert registered(origin, worktree_path(root, other)) and not (worktree_path(root, other) / "partial").exists()


def test_failed_preparation_retries_transient_git_failures_and_cleans_up(origin: Path, root: Path, tmp_path: Path) -> None:
    before = evidence(origin, tmp_path)
    wrapper = tmp_path / "flaky-git"
    counter = tmp_path / "failures"
    counter.write_text("1")
    wrapper.write_text(f'#!/bin/sh\ncase "$*" in *"worktree add"*) n=$(cat {counter}); '
                       f'if [ "$n" -gt 0 ]; then echo $((n-1)) > {counter}; exit 1; fi;; esac\nexec git "$@"\n')
    wrapper.chmod(0o755)
    worktree = adapter(root, git=str(wrapper))

    with pytest.raises(ServiceError) as error:
        run(worktree.provision(OWNERSHIP, spec(origin)))
    assert error.value.code == "UNAVAILABLE" and error.value.retryable
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    assert registered(origin, worktree_path(root)) == [f"worktree {worktree_path(root)}"]

    missing_tool = spec(origin)
    missing_tool["spec"]["tools"] = ["oe-missing-tool"]
    with pytest.raises(ServiceError) as error:
        run(adapter(root).provision(Ownership("ws_b", "rt_b", 1), missing_tool))
    assert error.value.code == "PROVIDER_ERROR" and (root / "rt_b" / "owner.json").exists()
    run(adapter(root).destroy(Ownership("ws_b", "rt_b", 1), None))  # failed preparation cleans up
    run(adapter(root).destroy(OWNERSHIP, locator))
    assert os.listdir(root) == [] and evidence(origin, tmp_path) == before


def test_foreign_or_unrecorded_directories_and_symlinked_runtime_directories_are_never_adopted(
    origin: Path, root: Path, tmp_path: Path,
) -> None:
    worktree = adapter(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")

    (root / "rt_a").mkdir()
    (root / "rt_a" / "theirs").write_text("theirs")
    for call in (worktree.provision(OWNERSHIP, spec(origin)), worktree.destroy(OWNERSHIP, None)):
        with pytest.raises(OwnershipMismatch):
            run(call)
    assert os.listdir(root / "rt_a") == ["theirs"]

    (root / "rt_a" / "owner.json").write_text(json.dumps({"provider": "process", "workspace_id": "ws_other",
                                                          "runtime_id": "rt_a", "generation": 1, "repositories": []}))
    with pytest.raises(OwnershipMismatch):
        run(worktree.provision(OWNERSHIP, spec(origin)))

    # A record naming a worktree outside its own directory is not ours either.
    (root / "rt_a" / "owner.json").write_text(json.dumps({"provider": "process", "workspace_id": "ws_a", "runtime_id": "rt_a",
                                                          "generation": 1, "repositories": [
                                                              {"source": str(origin), "revision": "x", "path": str(outside)}]}))
    with pytest.raises(OwnershipMismatch):
        run(worktree.destroy(OWNERSHIP, None))

    subprocess.run(["rm", "-rf", str(root / "rt_a")], check=True)
    (root / "rt_a").symlink_to(outside)
    for call in (worktree.provision(OWNERSHIP, spec(origin)), worktree.destroy(OWNERSHIP, None)):
        with pytest.raises(OwnershipMismatch):
            run(call)
    assert os.listdir(outside) == ["keep"] and (outside / "keep").read_text() == "keep"


def test_context_files_cannot_escape_through_symlinks(origin: Path, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    feature = tmp_path / "unrelated"  # the caller's worktree with `feature` checked out
    os.symlink(outside, feature / "link")
    git(feature, "add", "link")
    git(feature, "commit", "-q", "-m", "link")
    worktree = adapter(root)

    for path in ("repositories/0/link/escaped", "repositories/0/link"):
        ownership = Ownership("ws_" + path[-4:], "rt_" + path[-4:], 1)
        with pytest.raises(ServiceError) as error:
            run(worktree.provision(ownership, spec(origin, files=[{"path": path, "content": "x"}])))
        assert error.value.code == "PROVIDER_ERROR" and "beneath the workspace root" in error.value.message
        assert not (root / ownership.runtime_id / "ready").exists()
    assert os.listdir(outside) == []


def test_stale_git_metadata_fails_ensure_and_destroy_removes_only_what_is_provably_owned(
    origin: Path, root: Path, tmp_path: Path,
) -> None:
    before = evidence(origin, tmp_path)
    worktree = adapter(root)
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    path = worktree_path(root)

    # The directory vanished after it was ready: ensure never recreates it.
    subprocess.run(["rm", "-rf", str(path)], check=True)
    with pytest.raises(ServiceError) as error:
        run(worktree.ensure(OWNERSHIP, locator, spec(origin)))
    assert error.value.code == "PROVIDER_ERROR" and not error.value.retryable
    run(worktree.destroy(OWNERSHIP, locator))  # the stale registration is removed with the runtime
    assert registered(origin, path) == [] and not (root / "rt_a").exists()

    # Git's metadata vanished after it was ready: the directory is ambiguous, so it is kept.
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    git(origin, "worktree", "unlock", str(path))
    admin = Path(git(path, "rev-parse", "--path-format=absolute", "--git-dir").strip())
    subprocess.run(["rm", "-rf", str(admin)], check=True)
    with pytest.raises(ServiceError):
        run(worktree.ensure(OWNERSHIP, locator, spec(origin)))
    with pytest.raises(ServiceError) as error:
        run(worktree.destroy(OWNERSHIP, locator))
    assert "no Git metadata" in error.value.message and (path / "README").exists()
    run(adapter(root, discard_changes=True).destroy(OWNERSHIP, locator))
    assert not (root / "rt_a").exists() and evidence(origin, tmp_path) == before


def test_a_generation_the_directory_was_not_created_for_is_never_adopted(origin: Path, root: Path) -> None:
    worktree = adapter(root)
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    rebound = Ownership(OWNERSHIP.workspace_id, OWNERSHIP.runtime_id, OWNERSHIP.generation + 1)

    # The daemon's journal is sealed to generation 1; generation 2 must not adopt, restart or remove it.
    for call in (worktree.provision(rebound, spec(origin)), worktree.ensure(rebound, locator, spec(origin)),
                 worktree.destroy(rebound, locator)):
        with pytest.raises(OwnershipMismatch):
            run(call)
    assert registered(origin, worktree_path(root)) and (root / "rt_a" / "owner.json").exists()
    run(worktree.destroy(OWNERSHIP, locator))


def test_files_outside_the_worktrees_that_vcp_did_not_write_are_never_silently_deleted(origin: Path, root: Path) -> None:
    worktree = adapter(root)
    specification = spec(origin, instructions="x", files=[{"path": "notes/todo.md", "content": "x\n"}])
    locator = run(worktree.provision(OWNERSHIP, specification))
    workspace = root / "rt_a" / "workspace"

    for path, change in (("scratch.txt", lambda: (workspace / "scratch.txt").write_text("agent output")),
                         ("notes/todo.md", lambda: ((workspace / "scratch.txt").unlink(),
                                                    (workspace / "notes" / "todo.md").write_text("edited"))),
                         ("notes/more", lambda: ((workspace / "notes" / "todo.md").write_text("x\n"),
                                                 (workspace / "notes" / "more").mkdir()))):
        change()
        with pytest.raises(ServiceError) as error:
            run(worktree.destroy(OWNERSHIP, locator))
        assert error.value.code == "PROVIDER_ERROR" and error.value.details["path"] == path
        assert (root / "rt_a" / "owner.json").exists() and registered(origin, worktree_path(root))

    # Ignored files inside a worktree are the agent's work too.
    (workspace / "notes" / "more").rmdir()
    admin = Path(git(worktree_path(root), "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    (admin / "info").mkdir(exist_ok=True)
    (admin / "info" / "exclude").write_text("build/\n")
    (worktree_path(root) / "build").mkdir()
    (worktree_path(root) / "build" / "output").write_text("built")
    with pytest.raises(ServiceError) as error:
        run(worktree.destroy(OWNERSHIP, locator))
    assert "ignored" in error.value.message
    (admin / "info" / "exclude").write_text("")

    run(adapter(root, discard_changes=True).destroy(OWNERSHIP, locator))
    assert not (root / "rt_a").exists()


def test_removal_never_follows_a_parent_swapped_for_a_symlink(origin: Path, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "repositories" / "0").mkdir(parents=True)
    (outside / "repositories" / "0" / "keep").write_text("keep")
    worktree = adapter(root, discard_changes=True)
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    workspace = root / "rt_a" / "workspace"
    workspace.rename(root / "rt_a" / "moved")
    workspace.symlink_to(outside)

    for call in (worktree.ensure(OWNERSHIP, locator, spec(origin)), worktree.destroy(OWNERSHIP, locator)):
        with pytest.raises(ServiceError) as error:
            run(call)
        assert "symlink" in error.value.message
    assert (outside / "repositories" / "0" / "keep").read_text() == "keep"
    assert (root / "rt_a" / "owner.json").exists()


def test_dirty_or_unreferenced_work_is_refused_and_branches_are_never_deleted(origin: Path, root: Path, tmp_path: Path) -> None:
    before = evidence(origin, tmp_path)
    worktree = adapter(root)
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    path = worktree_path(root)

    for dirty in ("modified", "untracked"):
        if dirty == "modified":
            (path / "README").write_text("agent edit\n")
        else:
            git(path, "checkout", "-q", "--", "README")
            (path / "new.txt").write_text("agent file\n")
        with pytest.raises(ServiceError) as error:
            run(worktree.destroy(OWNERSHIP, locator))
        assert error.value.code == "PROVIDER_ERROR" and "uncommitted" in error.value.message
        assert (root / "rt_a" / "owner.json").exists() and registered(origin, path)

    git(path, "add", "new.txt")
    git(path, "commit", "-q", "-m", "agent commit")
    with pytest.raises(ServiceError) as error:
        run(worktree.destroy(OWNERSHIP, locator))
    assert "no branch or tag contains" in error.value.message

    # The agent saves its work on a branch it creates: destroy keeps the branch and removes the worktree.
    git(path, "branch", "agent/work")
    run(worktree.destroy(OWNERSHIP, locator))
    assert registered(origin, path) == [] and not (root / "rt_a").exists()
    assert "new.txt" in git(origin, "ls-tree", "--name-only", "agent/work")
    git(origin, "branch", "-q", "-D", "agent/work")
    assert evidence(origin, tmp_path) == before

    # Explicit operator authorization discards uncommitted work, and still deletes no branch.
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    (path / "README").write_text("agent edit\n")
    run(adapter(root, discard_changes=True).destroy(OWNERSHIP, locator))
    assert registered(origin, path) == [] and evidence(origin, tmp_path) == before


def test_destroy_refuses_an_unrelated_worktree_placed_inside_the_workspace(origin: Path, root: Path) -> None:
    worktree = adapter(root)
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    foreign = root / "rt_a" / "workspace" / "someone-elses"
    git(origin, "worktree", "add", "-q", "--detach", str(foreign), "main")

    with pytest.raises(ServiceError) as error:
        run(worktree.destroy(OWNERSHIP, locator))

    assert "unrelated Git worktree" in error.value.message
    assert registered(origin, worktree_path(root)) and registered(origin, foreign)


def test_a_foreign_lock_on_our_path_is_an_ownership_mismatch(origin: Path, root: Path) -> None:
    worktree = adapter(root)
    run(worktree.provision(OWNERSHIP, spec(origin)))
    path = worktree_path(root)
    git(origin, "worktree", "unlock", str(path))
    git(origin, "worktree", "lock", "--reason", "someone else", str(path))

    for call in (worktree.ensure(OWNERSHIP, {}, spec(origin)), worktree.destroy(OWNERSHIP, None)):
        with pytest.raises((OwnershipMismatch, ServiceError)):
            run(call)
    assert registered(origin, path) and (root / "rt_a" / "owner.json").exists()


# -- daemon supervision --------------------------------------------------------


def starts(root: Path) -> list[list[str]]:
    log = root / "rt_a" / "state" / "starts.jsonl"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def supervised(root: Path, **options) -> Worktree:
    return adapter(root, daemon=HostDaemon("nats://127.0.0.1:1", local_test_account=True, command=FAKE_DAEMON,
                                           stop_timeout=options.pop("stop_timeout", 5.0),
                                           inherit=(*INHERITED_VARIABLES, "OE_TEST_DAEMON_IGNORE_TERM")), **options)


def test_the_daemon_is_started_once_rediscovered_after_restart_and_stopped_before_removal(origin: Path, root: Path) -> None:
    worktree = supervised(root)
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    journal = root / "rt_a" / "state"

    assert HostDaemon.running(journal)
    [first] = starts(root)
    assert "--initialize" in first and first[first.index("--workspace-root") + 1] == str(root / "rt_a" / "workspace")
    assert first[first.index("--journal-directory") + 1] == str(journal)

    # A restarted VCP rediscovers the running daemon instead of starting another.
    run(supervised(root).ensure(OWNERSHIP, locator, spec(origin)))
    assert len(starts(root)) == 1

    # A daemon that died is restarted for the established journal, never re-initialized.
    pid = json.loads((root / "rt_a" / "daemon.json").read_text())["pid"]
    os.killpg(pid, signal.SIGKILL)
    while HostDaemon.running(journal):
        pass
    run(supervised(root).ensure(OWNERSHIP, locator, spec(origin)))
    assert len(starts(root)) == 2 and "--initialize" not in starts(root)[1]

    run(supervised(root).destroy(OWNERSHIP, locator))
    assert not (root / "rt_a").exists() and registered(origin, worktree_path(root)) == []


def test_destroy_escalates_to_sigkill_and_keeps_ownership_when_termination_is_unconfirmed(
    origin: Path, root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OE_TEST_DAEMON_IGNORE_TERM", "1")
    worktree = supervised(root, stop_timeout=0.5)
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    journal = root / "rt_a" / "state"

    signals = []

    async def unanswered(pid, signum):
        signals.append(signum)

    monkeypatch.setattr(daemon_module.HostDaemon, "_signal", staticmethod(unanswered))
    with pytest.raises(ServiceError) as error:
        run(worktree.destroy(OWNERSHIP, locator))
    assert error.value.code == "UNAVAILABLE" and error.value.retryable
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert HostDaemon.running(journal) and registered(origin, worktree_path(root)) and (root / "rt_a" / "owner.json").exists()

    monkeypatch.undo()
    run(supervised(root, stop_timeout=0.5).destroy(OWNERSHIP, locator))  # SIGTERM ignored, SIGKILL confirmed
    assert not (root / "rt_a").exists()


def test_the_daemon_gets_a_minimal_environment(origin: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_TEST_CONTROL_PLANE_SECRET", "s3cret")
    monkeypatch.setenv("OE_TEST_DAEMON_IGNORE_TERM", "0")
    locator = run(supervised(root).provision(OWNERSHIP, spec(origin)))
    pid = json.loads((root / "rt_a" / "daemon.json").read_text())["pid"]

    environment = subprocess.run(["ps", "-ww", "-E", "-o", "command=", "-p", str(pid)] if sys.platform == "darwin"
                                 else ["cat", f"/proc/{pid}/environ"], capture_output=True, text=True).stdout
    assert "OE_TEST_DAEMON_IGNORE_TERM" in environment and "OE_TEST_CONTROL_PLANE_SECRET" not in environment
    run(supervised(root).destroy(OWNERSHIP, locator))


def test_a_daemon_started_before_its_record_was_written_is_still_found_and_stopped(origin: Path, root: Path) -> None:
    worktree = supervised(root)
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    (root / "rt_a" / "daemon.json").unlink()  # VCP crashed between starting the daemon and recording it
    journal = root / "rt_a" / "state"

    run(supervised(root).destroy(OWNERSHIP, locator))
    assert not HostDaemon.running(journal) and not (root / "rt_a").exists()


def test_an_unidentifiable_journal_lock_holder_is_never_signalled(origin: Path, root: Path) -> None:
    worktree = supervised(root)
    locator = run(worktree.provision(OWNERSHIP, spec(origin)))
    journal = root / "rt_a" / "state"
    run(worktree.daemon.stop(root / "rt_a", journal))
    # Something other than a daemon for this journal (here, this test) holds the lock.
    lock = os.open(journal / ".daemon.lock", os.O_RDWR)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    record = root / "rt_a" / "daemon.json"
    record.write_text(json.dumps({**json.loads(record.read_text()), "pid": os.getpid()}))
    try:
        with pytest.raises(ServiceError) as error:
            run(worktree.destroy(OWNERSHIP, locator))
        assert error.value.code == "UNAVAILABLE" and HostDaemon.running(journal)
    finally:
        os.close(lock)
    run(worktree.destroy(OWNERSHIP, locator))
    assert not (root / "rt_a").exists()

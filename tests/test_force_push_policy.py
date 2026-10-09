"""Ownership must be established before any force push or metadata amendment."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from engine.adapters.source_control.github import GitHubSourceControl
from engine.domain.ids import WorkspaceId
from engine.ports import GitResult
from engine.runtime.push_policy import push_spec


OWNED = (("acme/api", 7),)


def source_for(branch="agent/change", *, default="main", protected=False, state="open", base="main", head_repo=1, integration=False):
    source = GitHubSourceControl("")
    source._root_path = AsyncMock(return_value="/checkout")
    source._git_checked = AsyncMock(return_value="https://github.com/acme/api.git")
    source._git = AsyncMock(return_value=GitResult(exit_code=0, stdout="", stderr=""))

    async def api(method, path, **kwargs):
        if path == "/repos/acme/api":
            return {"id": 1, "default_branch": default}
        if path == "/repos/acme/api/pulls/7":
            return {"state": state, "head": {"ref": branch, "repo": {"id": head_repo}}, "base": {"ref": base, "repo": {"id": 1}}}
        if path == "/repos/acme/api/pulls":
            return [{"number": 99}] if integration else []
        if "/branches/" in path:
            return {"name": branch, "protected": protected}
        raise AssertionError(path)

    source._api = AsyncMock(side_effect=api)
    return source


@pytest.mark.parametrize("options,ref", [(["--force"], "HEAD:agent/change"), (["-f"], "HEAD:agent/change"), (["--force-with-lease"], "HEAD:agent/change"), (["--force-with-lease=refs/heads/agent/change:abc"], "HEAD:agent/change"), ([], "+HEAD:refs/heads/agent/change")])
@pytest.mark.parametrize("owned", [(), (("acme/other", 7),), (("acme/api", 8),), OWNED])
def test_force_push_requires_exact_owned_pr(options, ref, owned):
    source = source_for()
    call = source.run_git(WorkspaceId("ws"), ["push", *options, "origin", ref], owned_pull_requests=owned)
    if owned == OWNED:
        asyncio.run(call)
        source._git.assert_awaited_once()
    else:
        with pytest.raises((ValueError, AssertionError)):
            asyncio.run(call)
        source._git.assert_not_awaited()


@pytest.mark.parametrize("changes", [
    {"branch": "main"}, {"branch": "release/stable"},
    {"default": "agent/change"}, {"base": "agent/change"},
    {"protected": True}, {"protected": None}, {"state": "closed"},
    {"head_repo": 2}, {"default": ""}, {"integration": True},
])
def test_owned_pr_does_not_authorize_nonfeature_branch(changes):
    source = source_for(**changes)
    branch = changes.get("branch", "agent/change")
    with pytest.raises(ValueError):
        asyncio.run(source.run_git(WorkspaceId("ws"), ["push", "--force", "origin", f"HEAD:{branch}"], owned_pull_requests=OWNED))
    source._git.assert_not_awaited()


@pytest.mark.parametrize("arguments", [
    ["push", "--for", "origin", "agent/change"],
    ["push", "-uf", "origin", "agent/change"],
    ["push", "--mirror", "origin"],
    ["push", "--all", "origin"],
    ["push", "--repo=other", "origin", "agent/change"],
    ["push", "origin", "+refs/tags/v1:refs/tags/v1"],
    ["push", "origin", "+refs/heads/*:refs/heads/*"],
])
def test_ambiguous_or_bulk_force_push_forms_are_rejected(arguments):
    with pytest.raises(ValueError):
        push_spec(arguments)


def test_every_destination_is_checked_before_push():
    source = source_for()
    with pytest.raises(ValueError):
        asyncio.run(source.run_git(WorkspaceId("ws"), ["push", "--force", "origin", "HEAD:agent/change", "HEAD:main"], owned_pull_requests=OWNED))
    source._git.assert_not_awaited()


@pytest.mark.parametrize("failure", ["api", "multiple_remotes"])
def test_force_push_fails_closed_when_eligibility_cannot_be_verified(failure):
    source = source_for()
    if failure == "api":
        source._api.side_effect = RuntimeError("offline")
    else:
        source._git_checked.return_value = "https://github.com/acme/api.git\nhttps://github.com/acme/other.git"
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(source.run_git(WorkspaceId("ws"), ["push", "--force", "origin", "agent/change"], owned_pull_requests=OWNED))
    source._git.assert_not_awaited()


def test_normal_push_needs_no_pr_and_disables_configured_mirroring():
    source = source_for()
    asyncio.run(source.run_git(WorkspaceId("ws"), ["push", "origin", "agent/change"]))
    source._api.assert_not_awaited()
    assert "--no-mirror" in source._git.await_args.args[1]


@pytest.mark.parametrize("owned", [(), OWNED])
def test_metadata_uses_normal_push_without_requiring_an_existing_pr(owned):
    source = source_for()
    async def git(root, args):
        return {
            ("branch", "--show-current"): "agent/change",
            ("rev-parse", "HEAD"): "abc",
            ("log", "-1", "--format=%B"): "feat: change",
            ("ls-remote", "origin", "refs/heads/agent/change"): "abc\trefs/heads/agent/change",
            ("remote", "get-url", "--push", "--all", "origin"): "https://github.com/acme/api.git",
        }.get(args, "")
    source._git_checked.side_effect = git
    call = source._issue_head("/checkout", "agent/change", "main", "#7", "resolves", owned_pull_requests=owned)
    asyncio.run(call)
    commands = [call.args[1] for call in source._git_checked.await_args_list]
    assert any("commit" in args and "--allow-empty" in args for args in commands)
    assert any("push" in args for args in commands)
    assert not any("--amend" in args or any(arg.startswith("--force") for arg in args) for args in commands)
    source._api.assert_not_awaited()


def test_prepared_initial_issue_head_needs_no_force_push():
    source = source_for()
    async def git(root, args):
        return {("branch", "--show-current"): "agent/change", ("rev-parse", "HEAD"): "abc", ("log", "-1", "--format=%B"): "feat: change\n\nResolves #7\n\nRefs #7", ("ls-remote", "origin", "refs/heads/agent/change"): "abc\trefs/heads/agent/change"}.get(args, "")
    source._git_checked.side_effect = git
    asyncio.run(source._issue_head("/checkout", "agent/change", "main", "#7", "resolves"))
    source._api.assert_not_awaited()
    assert not any("--amend" in call.args[1] for call in source._git_checked.await_args_list)

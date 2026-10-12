"""Public git-safety contract: add one adapter parameter to cover a new forge.

Mock only the process boundary so each adapter must apply its guards and pass
a sanitized environment to git. No forge API or remote is contacted.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.source_control.gitlab import GitLabSourceControl
from engine.domain.ids import WorkspaceId
from engine.runtime.git_core import (
    GitGlobalOptionError,
    InternalBranchPublicationError,
    UnsafePushSpecificationError,
)


WORKSPACE = WorkspaceId("git-contract")
ADAPTER_SECRET = "adapter-only-secret"
FORGE_CREDENTIALS = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN", "GITLAB_TOKEN", "GL_TOKEN", "GITLAB_ACCESS_TOKEN",
)


@pytest.fixture(params=[
    pytest.param(GitHubSourceControl, id="github"),
    pytest.param(GitLabSourceControl, id="gitlab"),
])
def source(request, tmp_path):
    workspace = AsyncMock()
    workspace.root_path.return_value = str(tmp_path)
    return request.param(
        ADAPTER_SECRET, workspace_provider=workspace, git_binary_path="contract-git",
        transport=AsyncMock(),
    )


@pytest.fixture
def spawn(monkeypatch):
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b"ok\n", b"")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return spawn


@pytest.mark.parametrize("prefix", [[], ["--no-pager"]])
@pytest.mark.parametrize("command", ["send-pack", "http-push"])
def test_publication_plumbing_cannot_bypass_push_guard(source, spawn, prefix, command):
    with pytest.raises(ValueError, match="use git push"):
        asyncio.run(source.run_git(WORKSPACE, [*prefix, command, "origin", "agent/topic"]))
    spawn.assert_not_called()


@pytest.mark.parametrize("prefix", [[], ["--no-pager"]])
@pytest.mark.parametrize("force", ["", "+"])
@pytest.mark.parametrize("qualified", ["", "refs/heads/"])
@pytest.mark.parametrize("source_ref", ["", "HEAD:", "agent/topic:", "engine/private:"])
def test_push_refuses_every_internal_destination_spelling(
    source, spawn, prefix, force, qualified, source_ref,
):
    refspec = f"{force}{source_ref}{qualified}engine/private"
    with pytest.raises(InternalBranchPublicationError, match="internal Engine branch"):
        asyncio.run(source.run_git(WORKSPACE, [*prefix, "push", "origin", refspec]))
    spawn.assert_not_called()


@pytest.mark.parametrize("arguments", [
    ["push"],
    ["push", "origin"],
    ["push", "origin", "HEAD"],
    ["push", "origin", "@"],
    ["push", "--all", "origin"],
    ["push", "--branches", "origin"],
    ["push", "--mirror", "origin"],
    ["push", "origin", ":"],
    ["push", "origin", "+:"],
    ["push", "origin", "refs/heads/*:refs/heads/*"],
    ["push", "origin", "agent/topic:"],
])
def test_implicit_and_bulk_pushes_are_refused(source, spawn, arguments):
    with pytest.raises(UnsafePushSpecificationError):
        asyncio.run(source.run_git(WORKSPACE, arguments))
    spawn.assert_not_called()


@pytest.mark.parametrize("arguments", [
    ["-c", "alias.publish=!git push", "publish"],
    ["--config-env=alias.publish=PAYLOAD", "publish"],
    ["--exec-path=/other/bin", "push", "origin", "agent/topic"],
    ["-C", "/other/checkout", "push", "origin", "agent/topic"],
    ["--git-dir=/other/.git", "push", "origin", "agent/topic"],
])
def test_global_options_cannot_bypass_the_boundary(source, spawn, arguments):
    with pytest.raises(GitGlobalOptionError):
        asyncio.run(source.run_git(WORKSPACE, arguments))
    spawn.assert_not_called()


@pytest.mark.parametrize("force", ["-f", "--force", "--force-with-lease", "+refspec"])
def test_force_push_requires_verified_ownership(source, spawn, force):
    arguments = ["push", force, "origin", "HEAD:agent/topic"]
    if force == "+refspec":
        arguments = ["push", "origin", "+HEAD:agent/topic"]
    with pytest.raises(ValueError):
        asyncio.run(source.run_git(WORKSPACE, arguments))
    spawn.assert_not_called()


@pytest.mark.parametrize("branch", [
    "engine/private", "+engine/private", "refs/heads/engine/private",
    "+refs/heads/engine/private",
])
@pytest.mark.parametrize("operation", ["publish", "request_review"])
def test_named_publication_methods_refuse_internal_branches(source, spawn, branch, operation):
    arguments = (WORKSPACE, branch)
    if operation == "request_review":
        arguments += ("main", "test: contract", "Body")
    with pytest.raises(InternalBranchPublicationError):
        asyncio.run(getattr(source, operation)(*arguments))
    spawn.assert_not_called()
    source._transport.request.assert_not_called()


@pytest.mark.parametrize("refspec", [
    "agent/topic", "HEAD:refs/heads/agent/topic",
    "engine/private:refs/heads/agent/topic",
])
def test_explicit_pushes_reach_git_with_mirroring_disabled(source, spawn, tmp_path, refspec):
    result = asyncio.run(source.run_git(
        WORKSPACE, ["--no-pager", "push", "origin", refspec],
    ))
    assert result.ok
    spawn.assert_awaited_once()
    assert spawn.call_args.args == (
        "contract-git", "-C", str(tmp_path), "--no-pager", "push",
        "--no-mirror", "origin", refspec,
    )


@pytest.mark.parametrize("operation", ["run_git", "create_branch", "commit_all", "publish"])
def test_no_forge_credential_reaches_any_git_subprocess(source, spawn, monkeypatch, operation):
    secrets = {name: f"host-secret-{name}" for name in FORGE_CREDENTIALS}
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GIT_CONTRACT_SENTINEL", "retained")
    arguments = {
        "run_git": (WORKSPACE, ["status"]),
        "create_branch": (WORKSPACE, "agent/topic", "main"),
        "commit_all": (WORKSPACE, "test: contract"),
        "publish": (WORKSPACE, "agent/topic"),
    }
    asyncio.run(getattr(source, operation)(*arguments[operation]))
    assert spawn.await_count >= 1
    for call in spawn.await_args_list:
        environment = call.kwargs["env"]
        assert not set(FORGE_CREDENTIALS).intersection(environment)
        assert not (set(secrets.values()) | {ADAPTER_SECRET}).intersection(environment.values())
        assert environment["GIT_CONTRACT_SENTINEL"] == "retained"

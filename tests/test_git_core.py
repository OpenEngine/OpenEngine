"""Both forge adapters enforce the same local git boundary."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from engine.adapters.source_control.github import GitHubSourceControl, GitHubSourceControlError
from engine.adapters.source_control.gitlab import GitLabSourceControl, GitLabSourceControlError
from engine.domain.ids import WorkspaceId
from engine.runtime import git_core


@pytest.fixture(params=[GitHubSourceControl, GitLabSourceControl])
def source(request):
    workspace = AsyncMock()
    workspace.root_path.return_value = "/checkout"
    return request.param("", workspace_provider=workspace, git_binary_path="custom-git")


@pytest.mark.parametrize("arguments", [
    ["--no-pager", "push", "origin", "HEAD:refs/heads/engine/private"],
    ["push", "origin", "+refs/heads/engine/private"],
    ["push", "origin", "HEAD"],
    ["push", "--mirror", "origin"],
    ["push", "origin", "refs/heads/*:refs/heads/*"],
    ["send-pack", "origin", "agent/topic"],
    ["http-push", "origin", "agent/topic"],
    ["-c", "alias.publish=!sh", "publish"],
])
def test_guards_reject_before_spawning(source, arguments, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(git_core.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises((ValueError, git_core.GitSourceControlError)):
        asyncio.run(source.run_git(WorkspaceId("test"), arguments))
    spawn.assert_not_called()


def test_shared_invoker_sanitizes_environment_and_guards_push(source, monkeypatch):
    tokens = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
              "GITHUB_ENTERPRISE_TOKEN", "GITLAB_TOKEN", "GL_TOKEN", "GITLAB_ACCESS_TOKEN")
    for name in tokens:
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("GIT_CORE_TEST", "retained")
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (b" pushed\n", b"")
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(git_core.asyncio, "create_subprocess_exec", spawn)
    result = asyncio.run(source.run_git(
        WorkspaceId("test"), ["--no-pager", "push", "origin", "HEAD:agent/topic"],
    ))
    assert result.ok and result.stdout == "pushed"
    assert spawn.call_args.args == (
        "custom-git", "-C", "/checkout", "--no-pager", "push", "--no-mirror",
        "origin", "HEAD:agent/topic",
    )
    environment = spawn.call_args.kwargs["env"]
    assert not set(tokens).intersection(environment)
    assert environment["GIT_CORE_TEST"] == "retained"


def test_spawn_errors_preserve_adapter_error_type(source, monkeypatch):
    monkeypatch.setattr(git_core.asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("missing")))
    error = GitHubSourceControlError if isinstance(source, GitHubSourceControl) else GitLabSourceControlError
    with pytest.raises(error, match="could not start custom-git"):
        asyncio.run(source.run_git(WorkspaceId("test"), ["status"]))


def test_checked_failure_preserves_adapter_error_type(source, monkeypatch):
    process = AsyncMock()
    process.returncode = 1
    process.communicate.return_value = (b"", b"checkout failed")
    monkeypatch.setattr(git_core.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    error = GitHubSourceControlError if isinstance(source, GitHubSourceControl) else GitLabSourceControlError
    with pytest.raises(error, match="checkout failed"):
        asyncio.run(source.create_branch(WorkspaceId("test"), "agent/topic", "main"))


@pytest.mark.parametrize("branch", ["engine/private", "+refs/heads/engine/private"])
def test_publish_rejects_normalized_internal_branch(source, branch):
    with pytest.raises(git_core.InternalBranchPublicationError):
        asyncio.run(source.publish(WorkspaceId("test"), branch))


def test_gitlab_still_refuses_force_push():
    source = GitLabSourceControl("")
    with pytest.raises(ValueError, match="cannot verify owned feature branches"):
        asyncio.run(source.run_git(WorkspaceId("test"), ["--no-pager", "push", "--force", "origin", "agent/topic"]))

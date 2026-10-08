"""Missing configured GitHub repositories become usable local checkouts."""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from engine.apps.web import __main__ as web_main
from engine.apps.web import repositories
from engine.ports.permissions import ApprovalCapability, PermissionScope
from engine.runtime import EngineConfigError, load_engine_config
from engine.runtime.approval_policy import PolicyDecision, policy_decision_for


def test_startup_clones_missing_repository_and_reuses_it(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = tmp_path / "engine.toml"
    config.write_text(f'[repos]\n"spiralsoft-ai/PATY" = "{tmp_path}/code/PATY"\n')
    calls = []

    def clone(args, **kwargs):
        calls.append(args)
        assert args[:4] == ["git", "clone", "--", "git@github.com:spiralsoft-ai/PATY.git"]
        assert kwargs["check"] and kwargs["timeout"] == 300
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        destination = Path(args[-1])
        destination.mkdir()
        (destination / "AGENTS.md").write_text("PATY instructions")

    monkeypatch.setattr(repositories.subprocess, "run", clone)
    loaded, _ = web_main.read_configuration(config)
    assert loaded.config.repos["spiralsoft-ai/PATY"] == str(tmp_path / "code/PATY")
    assert (tmp_path / "code/PATY/AGENTS.md").read_text() == "PATY instructions"
    web_main.read_configuration(config)
    assert len(calls) == 1
    assert set((tmp_path / "code").iterdir()) == {
        tmp_path / "code/PATY", tmp_path / "code/.engine-clone-PATY.lock",
    }


def test_concurrent_initializers_clone_missing_repository_once(tmp_path, monkeypatch):
    target = tmp_path / "repos/PATY"
    ready = Barrier(2)
    file_lock = repositories.FileLock
    calls = []

    def synchronized_lock(path):
        # Both initializers must observe the missing destination before locking.
        ready.wait(timeout=5)
        return file_lock(path)

    def clone(args, **kwargs):
        calls.append(args)
        destination = Path(args[-1])
        destination.mkdir()
        (destination / "AGENTS.md").write_text("complete checkout")

    monkeypatch.setattr(repositories, "FileLock", synchronized_lock)
    monkeypatch.setattr(repositories.subprocess, "run", clone)
    with ThreadPoolExecutor(max_workers=2) as executor:
        initializers = [
            executor.submit(
                repositories.ensure_repository_checkouts,
                {"spiralsoft-ai/PATY": str(target)},
            )
            for _ in range(2)
        ]
        for initializer in initializers:
            initializer.result(timeout=10)

    assert len(calls) == 1
    assert (target / "AGENTS.md").read_text() == "complete checkout"
    assert set(target.parent.iterdir()) == {
        target, target.parent / ".engine-clone-PATY.lock",
    }


@pytest.mark.parametrize("failure", [
    subprocess.CalledProcessError(128, "git"),
    subprocess.TimeoutExpired("git", 300),
    FileNotFoundError("git"),
])
def test_failed_clone_is_actionable_and_retryable(tmp_path, monkeypatch, failure):
    target = tmp_path / "repos/PATY"

    def fail(args, **kwargs):
        Path(args[-1]).mkdir()
        raise failure

    monkeypatch.setattr(repositories.subprocess, "run", fail)
    with pytest.raises(EngineConfigError, match="spiralsoft-ai/PATY.*GitHub SSH access"):
        repositories.ensure_repository_checkouts({"spiralsoft-ai/PATY": str(target)})
    assert not target.exists()
    assert list(target.parent.iterdir()) == [target.parent / ".engine-clone-PATY.lock"]


def test_existing_paths_and_local_aliases_are_untouched(tmp_path, monkeypatch):
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "local-work"
    marker.write_text("keep")
    dangling = tmp_path / "symlink"
    dangling.symlink_to(tmp_path / "absent")

    def unexpected(*args, **kwargs):
        pytest.fail("existing paths and aliases must not cause network access")

    monkeypatch.setattr(repositories.subprocess, "run", unexpected)
    repositories.ensure_repository_checkouts({
        "owner/existing": str(existing),
        "owner/link": str(dangling),
        "n8n": str(tmp_path / "n8n"),
        "../escape": str(tmp_path / "escape"),
    })
    assert marker.read_text() == "keep"
    assert not (tmp_path / "n8n").exists()


def test_relative_checkout_path_uses_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def clone(args, **kwargs):
        Path(args[-1]).mkdir()

    monkeypatch.setattr(repositories.subprocess, "run", clone)
    repositories.ensure_repository_checkouts({"owner/repo": "repos/repo"})
    assert (tmp_path / "repos/repo").is_dir()


@pytest.mark.parametrize("command", [
    "uv run pytest", "uv run pytest tests/unit",
    "uv run ruff check agent/ pipecat_outbound/",
    "uv run --directory mcp ruff check src/",
    "uv run ruff format --check src/",
    "uv run --directory cli ruff check --output-format=github .",
    "uv run --directory cli ruff format --check --diff .",
    "node test-local.mjs",
])
def test_paty_commands_have_explicit_approval(command):
    config = load_engine_config(Path(__file__).parents[1] / "engine.toml", environ={}).config
    assert config.repos["spiralsoft-ai/PATY"] == "~/code/PATY"
    policy = replace(config.approvals, auto_approve=False)
    assert policy_decision_for(policy, PermissionScope(ApprovalCapability.BASH, command)) is PolicyDecision.ALLOW


@pytest.mark.parametrize("command", ["uv run arbitrary", "npm run deploy", "node arbitrary.mjs"])
def test_paty_approvals_do_not_grant_arbitrary_commands(command):
    config = load_engine_config(Path(__file__).parents[1] / "engine.toml", environ={}).config
    policy = replace(config.approvals, auto_approve=False)
    assert policy_decision_for(policy, PermissionScope(ApprovalCapability.BASH, command)) is PolicyDecision.ASK

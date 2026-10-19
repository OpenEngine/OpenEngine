"""Preparation reports the source that will actually reach the agent."""

import asyncio
import json
from pathlib import Path
import subprocess

import pytest

from engine.adapters.workspace_provider.git_worktree import GitWorktreeWorkspaceProvider
from engine.graph_runtime_langgraph import ACPNode
from engine.runtime import workspace_preflight as preflight


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_inventory_reports_local_inputs_ignores_and_platform_constraints(git_repo):
    root = git_repo(commit=True)
    (root / "README.md").write_text("local edit")
    (root / ".gitignore").write_text("target/\n.env\n")
    (root / ".env").write_text("not included")
    (root / "target").mkdir()
    (root / "target/build").write_text("not included")
    project = root / "rust/snip"
    project.mkdir(parents=True)
    (project / "Cargo.toml").write_text("manifest")
    (project / "Overlay.swift").write_text("import AppKit")
    git(root, "add", "README.md")
    index = (root / ".git/index").read_bytes()
    report = preflight.inspect_workspace(root, local_changes=True)
    assert report["inputMode"] == "local working files"
    assert report["includedFiles"] == 4
    assert report["sourceBytes"] == sum((root / p).stat().st_size for p in
        ["README.md", ".gitignore", "rust/snip/Cargo.toml", "rust/snip/Overlay.swift"])
    assert report["untrackedFiles"] == 3
    assert report["modifiedFiles"] == 1
    assert report["ignoredEntries"] == 2
    assert report["projectMarkers"] == ["rust/snip/Cargo.toml"]
    assert any("macOS" in note for note in report["notes"])
    assert (root / ".git/index").read_bytes() == index


@pytest.mark.parametrize("budget", ["bytes", "entries"])
def test_oversized_source_fails_before_creating_run_worktree(git_repo, tmp_path, monkeypatch, budget):
    source = git_repo(commit=True)
    if budget == "bytes":
        monkeypatch.setattr(preflight, "MAX_SOURCE_BYTES", 1)
    else:
        monkeypatch.setattr(preflight, "MAX_SOURCE_MEMBERS", 1)
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "workspaces"))

    async def forbidden(*args, **kwargs):
        pytest.fail("oversized source must be rejected before making a checkout")

    monkeypatch.setattr(provider, "_checkout", forbidden)
    with pytest.raises(ValueError, match="sandbox limit.*Ignore"):
        asyncio.run(provider.provision(str(source), "HEAD", include_local_changes=True))


def test_selected_ref_inventory_does_not_describe_unrelated_local_files(git_repo, tmp_path):
    source = git_repo(commit=True)
    (source / "local-only.swift").write_text("not in selected ref")
    provider = GitWorktreeWorkspaceProvider(str(tmp_path / "workspaces"))
    workspace = asyncio.run(provider.provision(str(source), "main"))
    assert workspace.preflight is not None
    assert workspace.preflight["inputMode"] == "selected Git ref"
    assert workspace.preflight["untrackedFiles"] == 0
    assert "local-only.swift" not in workspace.preflight["topLevelEntries"]


def test_large_file_reporting_is_bounded_and_does_not_read_contents(git_repo, monkeypatch):
    source = git_repo(commit=True)
    monkeypatch.setattr(preflight, "LARGE_FILE_BYTES", 1)
    for i in range(30):
        (source / f"large-{i}.bin").write_bytes(b"x" * (i + 1))
    report = preflight.inspect_workspace(source, local_changes=True)
    assert len(report["largeFiles"]) == 5
    assert len(report["topLevelEntries"]) == 20
    assert report["largeFiles"][0] == {"path": "large-29.bin", "bytes": 30}


def test_planner_receives_inventory_for_text_and_content_block_prompts():
    summary = {"includedFiles": 4, "projectMarkers": ["rust/snip/Cargo.toml"]}
    state = {"workspaceSummary": summary}
    text = ACPNode(agent="test", cwd=".", prompt="Plan the task")._prompt(state)
    assert json.dumps(summary) in text
    assert text.endswith("Plan the task")
    blocks = [{"type": "text", "text": "Plan the task"}]
    actual = ACPNode(agent="test", cwd=".", prompt=lambda _: blocks)._prompt(state)
    assert actual[1:] == blocks
    assert json.dumps(summary) in actual[0]["text"]


def test_source_budget_matches_guest_transfer_budget():
    from engine.adapters.sandbox.smolvm import _guest
    assert preflight.MAX_SOURCE_BYTES == _guest.MAX_EXTRACTED_BYTES
    assert preflight.MAX_SOURCE_MEMBERS == _guest.MAX_MEMBERS


def test_plain_folder_reports_missing_git_repository(tmp_path):
    (tmp_path / "source.py").write_text("print('new project')")
    with pytest.raises(ValueError, match="initialized Git repository"):
        preflight.inspect_workspace(tmp_path, local_changes=True)


def test_missing_source_directory_has_an_actionable_error(tmp_path):
    with pytest.raises(ValueError, match="directory does not exist"):
        preflight.inspect_workspace(tmp_path / "missing", local_changes=True)


def test_empty_input_is_reported_without_blocking_a_new_project(git_repo):
    source = git_repo(commit=True)
    git(source, "rm", "README.md")
    report = preflight.inspect_workspace(source, local_changes=True)
    assert report["includedFiles"] == 0
    assert any("No source files" in note for note in report["notes"])

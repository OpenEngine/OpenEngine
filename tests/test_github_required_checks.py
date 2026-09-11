"""Required CI discovery without network or git subprocesses."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from engine.adapters.source_control.github import GitHubSourceControl, GitHubSourceControlError


@pytest.fixture
def source(monkeypatch):
    source = GitHubSourceControl("")
    monkeypatch.setattr(source, "_workspace_repo", AsyncMock(return_value=("owner", "repo")))
    responses = {
        "/pulls/42": {"head": {"sha": "head"}, "base": {"ref": "release/test"}},
        "/branches/release%2Ftest": {"protected": False},
        "/rules/branches/release%2Ftest": [],
        "/commits/head/check-runs": {"check_runs": []},
        "/commits/head/status": {"statuses": []},
        "/actions/runs": {"workflow_runs": []},
    }

    async def api(method, path, **kwargs):
        assert method == "GET"
        value = responses[path.removeprefix("/repos/owner/repo")]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(source, "_api", api)
    return source, responses


def snapshot(source):
    return asyncio.run(source.list_pipeline_status("workspace", change_request_number=42))


def test_confirmed_empty_requirements(source):
    adapter, _ = source
    assert snapshot(adapter).required_checks == ()


@pytest.mark.parametrize("with_status_checks", [False, True])
@pytest.mark.parametrize("conclusion", [None, "failure", "success"])
def test_workflow_rules_warn_and_fail_open(source, monkeypatch, caplog, with_status_checks, conclusion):
    from types import SimpleNamespace

    from engine.graph_runtime_langgraph.components import CICheck

    adapter, data = source
    rules = [{"type": "workflows", "parameters": {"workflows": [{
        "repository_id": 123, "path": ".github/workflows/required.yml",
        "ref": "refs/heads/main",
    }]}}]
    if with_status_checks:
        rules.insert(0, {"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "tests"}],
        }})
    data["/rules/branches/release%2Ftest"] = rules
    data["/commits/head/check-runs"] = {"check_runs": [{
        "name": "tests", "status": "completed", "conclusion": "success",
    }]}
    data["/actions/runs"] = {"workflow_runs": [{
        "id": 1, "name": "required", "path": ".github/workflows/required.yml",
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
    }]}
    execution = SimpleNamespace(
        runtime=SimpleNamespace(source_control=adapter), say=AsyncMock(),
    )
    monkeypatch.setattr(
        "engine.graph_runtime_langgraph.components.ci_check.current_execution",
        lambda: execution,
    )
    result = asyncio.run(CICheck()({
        "workspaceId": "workspace", "pr_url": "https://github.com/owner/repo/pull/42",
    }))
    assert result["ci_check"]["passed"] is True
    assert "Unsupported required workflows ruleset" in caplog.text
    assert "owner/repo:release/test" in caplog.text
    assert "failing open" in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)
    if with_status_checks:
        data["/commits/head/check-runs"]["check_runs"][0]["conclusion"] = "failure"
        result = asyncio.run(CICheck()({
            "workspaceId": "workspace", "pr_url": "https://github.com/owner/repo/pull/42",
        }))
        assert result["ci_check"]["passed"] is False


def test_classic_and_ruleset_requirements_include_missing_and_legacy(source):
    adapter, data = source
    data["/branches/release%2Ftest"] = {"protected": True, "protection": {
        "required_status_checks": {"contexts": ["legacy"]},
    }}
    data["/rules/branches/release%2Ftest"] = [{"type": "required_status_checks", "parameters": {
        "required_status_checks": [{"context": "security", "integration_id": 123}],
    }}]
    data["/commits/head/status"] = {"statuses": [{
        "context": "legacy", "state": "success", "target_url": "https://ci/legacy",
    }]}
    data["/commits/head/check-runs"] = {"check_runs": [{
        "name": "fast", "status": "completed", "conclusion": "success",
    }]}
    result = snapshot(adapter)
    assert [(c.name, c.status) for c in result.required_checks] == [
        ("legacy", "success"), ("security", "pending"),
    ]
    assert result.checks[0].details_url == "https://ci/legacy"
    data["/commits/head/check-runs"]["check_runs"].append({
        "name": "security", "status": "completed", "conclusion": "success", "app": {"id": 456},
    })
    assert snapshot(adapter).required_checks[1].status == "pending"
    data["/commits/head/check-runs"]["check_runs"][-1]["app"]["id"] = 123
    assert snapshot(adapter).required_checks[1].conclusion == "success"


def test_classic_app_requirement_cannot_be_satisfied_by_legacy(source):
    adapter, data = source
    data["/branches/release%2Ftest"] = {"protected": True, "protection": {
        "required_status_checks": {"contexts": ["tests"], "checks": [{"context": "tests", "app_id": 123}]},
    }}
    data["/commits/head/status"] = {"statuses": [{"context": "tests", "state": "success"}]}
    assert [(c.name, c.status) for c in snapshot(adapter).required_checks] == [("tests", "pending")]


def test_same_name_status_and_check_must_both_pass(source):
    adapter, data = source
    data["/branches/release%2Ftest"] = {"protected": True, "protection": {
        "required_status_checks": {"contexts": ["tests"]},
    }}
    data["/commits/head/status"] = {"statuses": [{"context": "tests", "state": "failure"}]}
    data["/commits/head/check-runs"] = {"check_runs": [{
        "name": "tests", "status": "completed", "conclusion": "success",
    }]}
    assert len(snapshot(adapter).required_checks) == 2
    assert snapshot(adapter).required_checks[1].status == "failure"


@pytest.mark.parametrize("path", ["/branches/release%2Ftest", "/rules/branches/release%2Ftest"])
def test_unreadable_requirements_do_not_mean_no_gates(source, path):
    adapter, data = source
    data[path] = GitHubSourceControlError("Forbidden")
    with pytest.raises(GitHubSourceControlError, match="Forbidden"):
        snapshot(adapter)


def test_required_status_on_later_page_is_included(source, monkeypatch):
    adapter, data = source
    data["/branches/release%2Ftest"] = {"protected": True, "protection": {
        "required_status_checks": {"contexts": ["last"]},
    }}
    original = adapter._api
    pages = []

    async def api(method, path, **kwargs):
        if path.endswith("/status"):
            page = kwargs["params"]["page"]
            pages.append(page)
            statuses = ([{"context": f"optional-{i}", "state": "success"} for i in range(100)]
                        if page == 1 else [{"context": "last", "state": "success"}])
            return {"total_count": 101, "statuses": statuses}
        return await original(method, path, **kwargs)

    monkeypatch.setattr(adapter, "_api", api)
    assert snapshot(adapter).required_checks[0].status == "success"
    assert pages == [1, 2]

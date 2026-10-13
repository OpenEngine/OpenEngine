"""API fix/review starts own their target, independently of its author."""

from unittest.mock import AsyncMock

import pytest

from engine.domain import RunId
from engine.graph_runtime import RunStatus
from engine.cli import starters
from engine.runtime import WorkflowCatalog, WorkOrdersConfig
from engine.runtime.workflows import load_workflow_file

from test_github_concierge import _graph_runtime
from web_fakes import RecordingCommunications


@pytest.fixture
def catalog(tmp_path):
    workflows = []
    for name in (starters.DEFAULT, "fix", "review"):
        path = tmp_path / f"{name}.py"
        path.write_text(starters.source(name))
        workflows.extend(load_workflow_file(path))
    return WorkflowCatalog.from_graphs(workflows)


@pytest.mark.parametrize("mode", ["fix", "review"])
@pytest.mark.parametrize("previous", [None, RunStatus.COMPLETED, RunStatus.FAILED])
def test_api_start_claims_target_and_refuses_second_run(mode, previous, github_app, client, catalog):
    runtime, opened = _graph_runtime(pr_number=99 if previous is None else 855,
                                     status=previous or RunStatus.RUNNING)
    app, _, _ = github_app(RecordingCommunications(), WorkOrdersConfig(), catalog,
                          graph_runtime=opened)
    request = {
        "workflowId": mode, "repository": "acme/api", "prompt": "Fix it",
        "inputs": {"pr_url": "https://github.com/Acme/API/pull/855"},
    }
    with client(app) as browser:
        response = browser.post("/api/runs", json=request)
        assert response.status_code == 201, response.text
        run_id = RunId(response.json()["runId"])
        assert browser.portal.call(runtime.store.pull_request_for_run, run_id) == ("acme/api", 855)
        assert runtime.store.claim_pull_request.await_args.kwargs == {
            "replacing": RunId("existing") if previous is not None else None,
        }
        runtime.cancel.assert_not_awaited()
        # Keep the first run active after the fake starts its second run.
        from engine.graph_runtime import GraphId, RunSnapshot
        runtime.snapshot = AsyncMock(side_effect=lambda asked: RunSnapshot(
            run_id=asked, graph_id=GraphId("implementation-review-v1"),
            status=RunStatus.RUNNING, values={}, pending_approvals=(),
        ))
        response = browser.post("/api/runs", json=request)
        assert response.status_code == 409, response.text
        assert f"PR #855 is held by {run_id}" in response.text
        runtime.cancel.assert_awaited_once_with(runtime.started_run_id)
        assert browser.portal.call(runtime.store.run_for_pull_request, "acme/api", 855) == run_id


@pytest.mark.parametrize("failure", ["race", "write"])
@pytest.mark.parametrize("mode", ["fix", "review"])
def test_api_cancels_run_when_claim_fails(mode, failure, github_app, client, catalog):
    runtime, opened = _graph_runtime(pr_number=99)
    runtime.store.claim_pull_request = AsyncMock(
        return_value=RunId("run-winner"),
        side_effect=OSError("database unavailable") if failure == "write" else None,
    )
    app, _, _ = github_app(RecordingCommunications(), WorkOrdersConfig(), catalog,
                          graph_runtime=opened)
    with client(app) as browser:
        response = browser.post("/api/runs", json={
            "workflowId": mode, "repository": "acme/api", "prompt": "Fix it",
            "inputs": {"pr_url": "https://github.com/acme/api/pull/855"},
        })
        assert response.status_code == (409 if failure == "race" else 503), response.text
        if failure == "race":
            assert "PR #855 is held by run-winner" in response.text
        runtime.cancel.assert_awaited_once_with(runtime.started_run_id)


@pytest.mark.parametrize("mode,inputs", [
    (starters.DEFAULT, {"implementation_runner": "codex", "review_runner": "claude"}),
    ("fix", {"ref": "topic"}),
    ("review", {"ref": "topic"}),
])
def test_unrelated_starts_do_not_claim(mode, inputs, github_app, client, catalog):
    runtime, opened = _graph_runtime()
    app, _, _ = github_app(RecordingCommunications(), WorkOrdersConfig(), catalog,
                          graph_runtime=opened)
    with client(app) as browser:
        response = browser.post("/api/runs", json={
            "workflowId": mode, "repository": "acme/api", "prompt": "Work",
            "inputs": inputs,
        })
        assert response.status_code == 201, response.text
        runtime.store.claim_pull_request.assert_not_awaited()


@pytest.mark.parametrize("mode", ["fix", "review"])
@pytest.mark.parametrize("url", [
    "https://github.com/other/private/pull/855",
    "https://other.example/acme/api/pull/855",
    "not-a-pull-request",
])
def test_target_must_belong_to_authorized_repository(mode, url, github_app, client, catalog):
    runtime, opened = _graph_runtime(pr_number=855, repository="other/private",
                                     status=RunStatus.COMPLETED)
    app, _, _ = github_app(RecordingCommunications(), WorkOrdersConfig(), catalog,
                          graph_runtime=opened)
    with client(app) as browser:
        response = browser.post("/api/runs", json={
            "workflowId": mode, "repository": "acme/api", "prompt": "Fix it",
            "inputs": {"pr_url": url},
        })
        assert response.status_code == 400, response.text
        runtime.start.assert_not_awaited()
        runtime.store.claim_pull_request.assert_not_awaited()
        assert browser.portal.call(runtime.store.run_for_pull_request, "other/private", 855) == "existing"


@pytest.mark.parametrize("failure", ["active", "race", "write", "repository"])
@pytest.mark.parametrize("mode", ["fix", "review"])
def test_scheduled_start_reports_claim_errors(mode, failure, github_app, client, catalog):
    from engine.graph_runtime import GraphId
    from engine.graph_runtime.metadata import RunMetadata

    runtime, opened = _graph_runtime(pr_number=855 if failure == "active" else 99)
    if failure in ("race", "write"):
        runtime.store.claim_pull_request = AsyncMock(
            return_value=RunId("run-winner"),
            side_effect=OSError("database unavailable") if failure == "write" else None,
        )
    app, _, _ = github_app(RecordingCommunications(), WorkOrdersConfig(), catalog,
                          graph_runtime=opened)
    with client(app) as browser:
        browser.portal.call(runtime.store.save_metadata, RunId("run-scheduled"),
                            GraphId(mode), RunMetadata(
                                scheduled=True, repository="acme/api", prompt="Fix it",
                                inputs={"pr_url":
                                        "https://github.com/other/private/pull/855" if failure == "repository"
                                        else "https://github.com/acme/api/pull/855"},
                            ))
        response = browser.post("/api/runs/run-scheduled/start")
        assert response.status_code == {"active": 409, "race": 409, "write": 503,
                                        "repository": 400}[failure], response.text
        if failure in ("active", "race"):
            holder = "existing" if failure == "active" else "run-winner"
            assert f"PR #855 is held by {holder}" in response.text
        if failure == "repository":
            runtime.start.assert_not_awaited()
            runtime.store.claim_pull_request.assert_not_awaited()
        else:
            runtime.cancel.assert_awaited_once_with(RunId("run-scheduled"))

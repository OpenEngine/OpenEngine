"""API fix/review starts own their target, independently of its author."""

from unittest.mock import AsyncMock

import pytest

from engine.domain import RunId
from engine.graph_runtime import RunStatus
from engine.graph_runtime.inputs import WorkflowInput
from engine.graph_runtime_langgraph import State, graph_workflow
from engine.runtime import WorkflowCatalog, WorkOrdersConfig
from langgraph.graph import END, START, StateGraph

from test_github_concierge import _graph_runtime
from web_fakes import RecordingCommunications


def _catalog():
    builder = StateGraph(State)
    builder.add_node("work", lambda state: {})
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    return WorkflowCatalog.from_graphs((graph_workflow(
        builder, id="implementation-review-v1", name="Fix or review",
        inputs=tuple(WorkflowInput(name, name) for name in (
            "pr_url", "fix_only", "review_only", "publish_review",
        )),
    ),))


@pytest.mark.parametrize("mode", ["fix_only", "review_only"])
@pytest.mark.parametrize("previous", [None, RunStatus.COMPLETED, RunStatus.FAILED])
def test_api_start_claims_target_and_refuses_second_run(mode, previous, github_app, client):
    runtime, opened = _graph_runtime(pr_number=99 if previous is None else 855,
                                     status=previous or RunStatus.RUNNING)
    app, _, _ = github_app(RecordingCommunications(), WorkOrdersConfig(), _catalog(),
                          graph_runtime=opened)
    request = {
        "workflowId": "implementation-review-v1", "repository": "acme/api", "prompt": "Fix it",
        "inputs": {mode: "true", "pr_url": "https://github.com/Acme/API/pull/855",
                   "publish_review": "true"},
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
def test_api_cancels_run_when_claim_fails(failure, github_app, client):
    runtime, opened = _graph_runtime(pr_number=99)
    runtime.store.claim_pull_request = AsyncMock(
        return_value=RunId("run-winner"),
        side_effect=OSError("database unavailable") if failure == "write" else None,
    )
    app, _, _ = github_app(RecordingCommunications(), WorkOrdersConfig(), _catalog(),
                          graph_runtime=opened)
    with client(app) as browser:
        response = browser.post("/api/runs", json={
            "workflowId": "implementation-review-v1", "repository": "acme/api", "prompt": "Fix it",
            "inputs": {"fix_only": "true", "pr_url": "https://github.com/acme/api/pull/855"},
        })
        assert response.status_code == (409 if failure == "race" else 503), response.text
        if failure == "race":
            assert "PR #855 is held by run-winner" in response.text
        runtime.cancel.assert_awaited_once_with(runtime.started_run_id)


@pytest.mark.parametrize("inputs", [
    {"pr_url": "https://github.com/acme/api/pull/855"},
    {"fix_only": "false", "pr_url": "https://github.com/acme/api/pull/855"},
    {"review_only": "true"},
])
def test_unrelated_starts_do_not_claim(inputs, github_app, client):
    runtime, opened = _graph_runtime()
    app, _, _ = github_app(RecordingCommunications(), WorkOrdersConfig(), _catalog(),
                          graph_runtime=opened)
    with client(app) as browser:
        response = browser.post("/api/runs", json={
            "workflowId": "implementation-review-v1", "repository": "acme/api", "prompt": "Work",
            "inputs": inputs,
        })
        assert response.status_code == 201, response.text
        runtime.store.claim_pull_request.assert_not_awaited()

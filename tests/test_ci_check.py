"""Deterministic CI polling and the implementation retry path."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.graph_runtime_langgraph.components import CICheck
from engine.ports import PipelineStatus, StatusCheck
from test_graph_workflow_definitions import definition_module, nodes_of

PR = "https://github.com/owner/repo/pull/42"
STATE = {"workspaceId": "ws-test", "pr_url": PR}


def status(state="completed", conclusion="success", ref="head"):
    return PipelineStatus(ref, (StatusCheck("tests", state, conclusion, "https://ci/job"),), ())


@pytest.fixture
def execution(monkeypatch):
    source = SimpleNamespace(list_pipeline_status=AsyncMock())
    execution = SimpleNamespace(runtime=SimpleNamespace(source_control=source), say=AsyncMock())
    monkeypatch.setattr(
        "engine.graph_runtime_langgraph.components.ci_check.current_execution",
        lambda: execution,
    )
    return execution


def test_waits_for_registration_and_completion(execution):
    source = execution.runtime.source_control
    source.list_pipeline_status.side_effect = [
        PipelineStatus("old", (), ()), status("in_progress", None), status(ref="new"),
    ]
    result = asyncio.run(CICheck(poll_interval=0)(STATE))["ci_check"]
    assert result["passed"] is True
    assert result["ref"] == "new"
    assert source.list_pipeline_status.await_count == 3
    for call in source.list_pipeline_status.call_args_list:
        assert call.args == ("ws-test",)
        assert call.kwargs == {"change_request_number": 42}


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_failures_include_actionable_feedback(execution, conclusion):
    execution.runtime.source_control.list_pipeline_status.return_value = status(conclusion=conclusion)
    result = asyncio.run(CICheck()(STATE))["ci_check"]
    assert result["passed"] is False
    assert conclusion in result["summary"]
    assert "https://ci/job" in result["summary"]


def test_waits_for_other_jobs_even_after_failure(execution):
    failed = status(conclusion="failure").checks[0]
    pending = status("queued", None).checks[0]
    execution.runtime.source_control.list_pipeline_status.side_effect = [
        PipelineStatus("head", (failed, pending), ()), status(conclusion="failure"),
    ]
    assert asyncio.run(CICheck(poll_interval=0)(STATE))["ci_check"]["passed"] is False
    assert execution.runtime.source_control.list_pipeline_status.await_count == 2


@pytest.mark.parametrize("error", [RuntimeError("API unavailable"), asyncio.CancelledError()])
def test_provider_errors_and_cancellation_propagate(execution, error):
    execution.runtime.source_control.list_pipeline_status.side_effect = error
    with pytest.raises(type(error)):
        asyncio.run(CICheck()(STATE))


def test_no_checks_times_out_instead_of_passing(execution):
    execution.runtime.source_control.list_pipeline_status.return_value = PipelineStatus("head", (), ())
    with pytest.raises(TimeoutError):
        asyncio.run(CICheck(poll_interval=0, timeout=0.01)(STATE))


@pytest.mark.parametrize("url", ["", "https://github.com/owner/repo/issues/42", "42"])
def test_missing_or_invalid_pr_is_rejected(execution, url):
    with pytest.raises(ValueError, match="pull request URL"):
        asyncio.run(CICheck()({**STATE, "pr_url": url}))
    execution.runtime.source_control.list_pipeline_status.assert_not_called()


def test_workflow_retries_same_pr_then_fans_out_to_review():
    module = definition_module()
    nodes = nodes_of(module.pipeline("codex"))
    assert isinstance(nodes[module.CI_CHECK], CICheck)
    failed = {**STATE, "task": "Fix bug", "ci_check": {"passed": False, "summary": "tests failed"}}
    assert module._after_ci(failed) == module.IMPLEMENTATION
    prompt = nodes[module.IMPLEMENTATION].prompt(failed)
    assert PR in prompt and "tests failed" in prompt
    assert "same PR branch" in prompt and "Do not open another" in prompt
    passed = {**failed, "ci_check": {"passed": True}}
    assert [send.node for send in module._after_ci(passed)] == [
        f"review-{facet.id}" for facet in module.REVIEW_FACETS
    ]


def test_graph_runs_implementation_again_before_review(execution):
    from langchain_core.runnables import RunnableLambda

    module = definition_module()
    builder = module.pipeline("codex")
    visited = []

    def stub(name):
        def run(state):
            visited.append(name)
            if name == module.IMPLEMENTATION:
                return {"pr_url": PR}
            return {}
        return RunnableLambda(run)

    for name, spec in builder.nodes.items():
        if name != module.CI_CHECK:
            spec.runnable = stub(name)
    execution.runtime.source_control.list_pipeline_status.side_effect = [
        status(conclusion="failure"), status(),
    ]
    result = asyncio.run(builder.compile().ainvoke(STATE))
    assert result["ci_check"]["passed"] is True
    assert visited[:4] == ["workspace", "naming", "implementation", "implementation"]
    assert visited.count("reranker") == 1
    assert visited[-1] == "human-review"


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_successful_and_nonblocking_checks_pass(execution, conclusion):
    execution.runtime.source_control.list_pipeline_status.return_value = status(conclusion=conclusion)
    assert asyncio.run(CICheck()(STATE))["ci_check"]["passed"] is True


def test_pipeline_status_without_conclusion(execution):
    from engine.ports import Pipeline

    execution.runtime.source_control.list_pipeline_status.return_value = PipelineStatus(
        "head", (), (Pipeline(1, "build", "failed", None, "https://ci/pipeline"),),
    )
    result = asyncio.run(CICheck()({**STATE, "pr_url": "https://gitlab.com/team/repo/-/merge_requests/42"}))["ci_check"]
    assert result["passed"] is False
    assert "https://ci/pipeline" in result["summary"]


def test_required_gate_waits_even_when_visible_checks_pass(execution):
    from dataclasses import replace

    pending = StatusCheck("security", "pending", None, "")
    passed = StatusCheck("security", "completed", "success", "https://ci/security")
    execution.runtime.source_control.list_pipeline_status.side_effect = [
        replace(status(), required_checks=(pending,)),
        replace(status(), required_checks=(passed,)),
    ]
    assert asyncio.run(CICheck(poll_interval=0)(STATE))["ci_check"]["passed"] is True
    assert execution.runtime.source_control.list_pipeline_status.await_count == 2


def test_confirmed_no_required_gates_passes_without_ci(execution):
    execution.runtime.source_control.list_pipeline_status.return_value = PipelineStatus(
        "head", (), (), required_checks=(),
    )
    assert asyncio.run(CICheck()(STATE))["ci_check"]["passed"] is True


@pytest.mark.parametrize("state,passed", [("success", True), ("failure", False), ("error", False)])
def test_required_legacy_status_controls_verdict(execution, state, passed):
    execution.runtime.source_control.list_pipeline_status.return_value = PipelineStatus(
        "head", (), (), required_checks=(StatusCheck("legacy", state, None, "https://ci/legacy"),),
    )
    assert asyncio.run(CICheck()(STATE))["ci_check"]["passed"] is passed


def test_missing_required_check_times_out(execution):
    execution.runtime.source_control.list_pipeline_status.return_value = PipelineStatus(
        "head", status().checks, (),
        required_checks=(StatusCheck("security", "pending", None, ""),),
    )
    with pytest.raises(TimeoutError):
        asyncio.run(CICheck(poll_interval=0, timeout=0.01)(STATE))

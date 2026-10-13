"""Deployment-controlled approval and the safety gates shared by both graphs."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.apps.web.composition import Settings, pull_request_approval_policy
from engine.graph_runtime.events import EventKind
from engine.graph_runtime_langgraph.components.change_graphs import Components, review
from engine.ports import ChangeRequest, PipelineStatus, StatusCheck
from engine.runtime.config import EngineConfigError, parse_engine_config
from test_graph_workflow_definitions import definition_module, nodes_of

PR = "https://github.com/owner/repo/pull/42"
STATE = {"repository": "owner/repo", "workspaceId": "ws", "pr_url": PR,
         "impact_level": "Green", "impact_rationale": "Only fixes a typo.", "review": []}


@pytest.fixture(params=["default", "component"])
def node(request):
    module = definition_module()
    if request.param == "default":
        builder = module.pipeline("codex")
        assert (module.IMPACT_ANALYSIS, "auto-approve") in builder.edges
        assert "auto-approve" in builder.branches
        assert module._after_impact_analysis(STATE) == module.HUMAN_REVIEW
    else:
        c = Components("codex", module.AGENTS, module.GitWorktreeWorkspaceProvider("/tmp/unused-auto-approve"))
        builder = review(c).builder
        assert ("impact", "auto-approve") in builder.edges
        assert ("auto-approve", "__end__") in builder.edges
    return nodes_of(builder)["auto-approve"]


@pytest.fixture
def execution(monkeypatch):
    source = SimpleNamespace(
        view_change_request=AsyncMock(return_value=ChangeRequest(
            42, "Fix", "open", "", "author", PR, "feature", "sha", "main")),
        list_pipeline_status=AsyncMock(return_value=PipelineStatus(
            "sha", (StatusCheck("tests", "completed", "success", ""),), (), ())),
        approve_change_request=AsyncMock(return_value=True),
    )
    execution = SimpleNamespace(runtime=SimpleNamespace(
        source_control=source, auto_approve_green=lambda _: True, repository_mode=None,
    ), say=AsyncMock(), emit=AsyncMock())
    monkeypatch.setattr("engine.graph_runtime_langgraph.components.auto_approve.current_execution", lambda: execution)
    return execution


@pytest.mark.parametrize("level,enabled,approved", [
    ("Green", True, True), ("Orange", True, False), ("Red", True, False), ("Green", False, False),
])
def test_rating_and_daemon_setting(node, execution, level, enabled, approved):
    execution.runtime.auto_approve_green = lambda _: enabled
    result = asyncio.run(node({**STATE, "impact_level": level}))["auto_approve"]
    assert result["approved"] is approved
    source = execution.runtime.source_control
    if approved:
        source.approve_change_request.assert_awaited_once_with(
            "owner/repo", 42,
            "Approved automatically because impact analysis rated this change Green.\n\nOnly fixes a typo.",
        )
        assert execution.emit.call_args.args[0] is EventKind.PULL_REQUEST_AUTO_APPROVED
    else:
        source.approve_change_request.assert_not_awaited()
        source.list_pipeline_status.assert_not_awaited()
    execution.say.assert_awaited_once()


def test_self_authored_skips_without_failing(node, execution):
    execution.runtime.source_control.approve_change_request.return_value = False
    result = asyncio.run(node(STATE))["auto_approve"]
    assert result["approved"] is False
    assert result["reason"] == "cannot approve own pull request"
    assert execution.emit.call_args.args[0] is EventKind.PULL_REQUEST_APPROVAL_SKIPPED


@pytest.mark.parametrize("update,reason", [
    ({"ci_check": {"passed": False}}, "CI failed"),
    ({"review": [{"body": "bug"}]}, "unresolved review findings"),
    ({"inputs": {"mode": "disconnected"}}, "disconnected mode"),
    ({"pr_url": ""}, "no pull request URL"),
    ({"impact_rationale": ""}, "missing impact rationale"),
])
def test_safety_gates(node, execution, update, reason):
    result = asyncio.run(node({**STATE, **update}))["auto_approve"]
    assert result["reason"] == reason
    execution.runtime.source_control.approve_change_request.assert_not_awaited()


@pytest.mark.parametrize("status", [
    PipelineStatus("sha", (StatusCheck("tests", "completed", "failure", ""),), (), ()),
    PipelineStatus("sha", (StatusCheck("tests", "in_progress", None, ""),), (), ()),
    PipelineStatus("sha", (), ()),
])
def test_fresh_ci_blocks_even_when_prior_check_passed(node, execution, status):
    execution.runtime.source_control.list_pipeline_status.return_value = status
    result = asyncio.run(node({**STATE, "ci_check": {"passed": True}}))["auto_approve"]
    assert result["approved"] is False
    assert "CI" in result["reason"]
    execution.runtime.source_control.approve_change_request.assert_not_awaited()


def test_review_target_and_gitlab_open_state(node, execution):
    source = execution.runtime.source_control
    source.view_change_request.return_value = replace(source.view_change_request.return_value, state="opened")
    result = asyncio.run(node({**STATE, "pr_url": "", "inputs": {"pr_url": PR}}))["auto_approve"]
    assert result["approved"] is True


def test_live_repository_mode_blocks(node, execution):
    execution.runtime.repository_mode = AsyncMock(return_value="disconnected")
    result = asyncio.run(node(STATE))["auto_approve"]
    assert "disconnected" in result["reason"]
    execution.runtime.source_control.approve_change_request.assert_not_awaited()


def test_provider_error_is_visible(node, execution):
    execution.runtime.source_control.approve_change_request.side_effect = RuntimeError("permission denied")
    result = asyncio.run(node(STATE))["auto_approve"]
    assert result["approved"] is False
    assert "permission denied" in result["reason"]


def test_config_defaults_and_overrides(tmp_path):
    assert parse_engine_config({}).pull_requests.auto_approve_green is False
    for global_value in (False, True):
        config = parse_engine_config({
            "repos": {"owner/repo": str(tmp_path), "offline": str(tmp_path / "offline")},
            "repo_modes": {"offline": "disconnected"},
            "pull_requests": {"auto_approve_green": global_value, "repos": {"owner/repo": not global_value}},
        })
        policy = pull_request_approval_policy(Settings(engine_config=config))
        assert policy("owner/repo") is not global_value
        assert policy(str(tmp_path)) is not global_value
        assert policy("other/repo") is global_value
        assert policy("offline") is False
        assert policy(str(tmp_path / "offline")) is False


@pytest.mark.parametrize("table", [
    {"auto_approve_green": "true"}, {"auto_approve_green": 1}, {"unknown": True},
    {"repos": {"missing": True}}, {"repos": {"owner/repo": "false"}},
])
def test_invalid_config(table):
    with pytest.raises(EngineConfigError):
        parse_engine_config({"repos": {"owner/repo": "/tmp/repo"}, "pull_requests": table})


def test_daemon_composition_binds_policy_to_runtime(tmp_path):
    from engine.apps.web.composition import build_graph_runtime

    config = parse_engine_config({"pull_requests": {"auto_approve_green": True}})
    settings = Settings(engine_config=config, graph_state_directory=str(tmp_path / "graphs"))

    async def scenario():
        async with build_graph_runtime(settings, ()) as runtime:
            assert runtime.auto_approve_green("owner/repo") is True

    asyncio.run(scenario())


def test_unresolved_forge_threads_block(node, execution):
    from engine.ports import Discussion

    source = execution.runtime.source_control
    source.view_change_request.return_value = replace(
        source.view_change_request.return_value,
        comments=(Discussion("reviewer", "Fix this", "", thread_id="thread", is_resolved=False),),
    )
    result = asyncio.run(node(STATE))["auto_approve"]
    assert result["reason"] == "unresolved review threads"
    source.approve_change_request.assert_not_awaited()

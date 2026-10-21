from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import reference
import resources
from resources import EXAMPLES, KINDS, load_documents, validate_resource, validate_text

EXAMPLE_FILES = sorted(EXAMPLES.glob("*.yaml"))


def _example(kind: str) -> dict[str, Any]:
    for path in EXAMPLE_FILES:
        for document in load_documents(path.read_text()):
            if document["kind"] == kind:
                return copy.deepcopy(document)
    pytest.fail(f"no {kind} example")


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.name)
def test_examples_validate(path: Path) -> None:
    assert validate_text(path.read_text()) == []


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_has_an_example(kind: str) -> None:
    assert _example(kind)["kind"] == kind


def test_checked_in_reference_is_current() -> None:
    path = resources.reference_path()
    assert path.exists() and path.read_text() == resources.page(), (
        "resources/reference.html is stale: run `uv run python packages/contracts/tooling/resources.py`"
    )


def test_reference_documents_every_kind_and_example() -> None:
    page = resources.page()
    for kind in KINDS:
        assert f'id="{kind.lower()}"' in page
    assert "implement-review" in page and "weekday-issues" in page and "open_pull_request" in page


def _problems(document: dict[str, Any]) -> list[str]:
    return [str(problem) for problem in validate_resource(document)]


def test_unknown_kind_and_version_are_refused() -> None:
    assert _problems({**_example("Tool"), "kind": "Widget"}) == ["kind: must be one of Graph, Loop, Tool"]
    assert _problems({**_example("Tool"), "apiVersion": "openengine.cc/v1"}) == [
        "apiVersion: must be openengine.cc/v2alpha1"
    ]


def test_resources_are_closed() -> None:
    graph = _example("Graph")
    graph["spec"]["plan"] = [{"name": "pick", "agent": {"runner": {"name": "claude"}, "prompt": "x", "promt": "y"}}]
    assert _problems(graph) == ["spec.plan[0].agent: Additional properties are not allowed ('promt' was unexpected)"]


def test_a_step_sets_exactly_one_kind() -> None:
    graph = _example("Graph")
    graph["spec"]["review"][0] = {"name": "both", "ci": {}, "human": {"prompt": "x"}}
    graph["spec"]["review"][1] = {"name": "neither"}
    assert _problems(graph) == [
        "spec.review[0]: set exactly one of agent, human, ci",
        "spec.review[1]: set exactly one of agent, human, ci",
    ]


def test_runner_choices_only_with_different_from() -> None:
    graph = _example("Graph")
    graph["spec"]["implementation"][1]["agent"]["runner"] = {"sameAs": "implement", "choices": ["codex"]}
    assert _problems(graph) == ["spec.implementation[1].agent.runner: choices cannot be set with sameAs"]


def test_step_names_must_work_in_expressions() -> None:
    graph = _example("Graph")
    graph["spec"]["implementation"][2]["name"] = "ci-check"
    assert _problems(graph) == ["spec.implementation[2].name: 'ci-check' does not match '^[a-z][a-z0-9_]{0,62}$'"]


@pytest.mark.parametrize(("interval", "ok"), [("1m", True), ("1h30m", True), ("365d", True), ("59s", False),
                                              ("366d", False), ("1x", False), ("m", False)])
def test_loop_interval_bounds(interval: str, ok: bool) -> None:
    loop = _example("Loop")
    loop["spec"]["schedule"] = {"interval": interval}
    assert (_problems(loop) == []) is ok


def test_time_zone_needs_cron() -> None:
    loop = _example("Loop")
    loop["spec"]["schedule"] = {"interval": "1h", "timeZone": "UTC"}
    assert _problems(loop) == ["spec.schedule: timeZone cannot be set with interval"]


def test_unquoted_timestamps_stay_text() -> None:
    (loop,) = load_documents("startsAt: 2026-11-02T08:00:00Z\n")
    assert loop == {"startsAt": "2026-11-02T08:00:00Z"}


def test_tool_handler_and_schemas() -> None:
    tool = _example("Tool")
    tool["spec"]["handler"] = {"builtin": {"name": "x"}, "exec": {"command": ["true"]}}
    tool["spec"]["inputSchema"] = {"type": "string"}
    assert _problems(tool) == [
        "spec.handler: set exactly one of builtin, exec, mcp",
        "spec.inputSchema.type: 'object' was expected",
    ]


def test_exec_working_dir_stays_in_the_workspace() -> None:
    tool = _example("Tool")
    for directory in ("/etc", "../up", "a/../../b"):
        tool["spec"]["handler"] = {"exec": {"command": ["true"], "workingDir": directory}}
        assert _problems(tool), directory


def test_problems_in_a_multi_document_file_name_the_document() -> None:
    text = "apiVersion: openengine.cc/v2alpha1\nkind: Tool\n---\nkind: Loop\n"
    assert [str(problem) for problem in validate_text(text)][0].startswith("[0]")
    assert [str(problem) for problem in validate_text("a: [")][0].startswith("(document): not YAML")


ROOT = resources.CONTRACTS.parents[2]


def test_pages_regenerates_and_publishes_the_resource_reference() -> None:
    workflow = (ROOT / ".github/workflows/pages.yml").read_text()
    assert "tooling/resources.py" in workflow
    assert "'v2/packages/contracts/**/reference.html'" in workflow
    assert "-name '*.schema.yaml'" in workflow


def test_site_reference_links_every_contract_and_kind() -> None:
    sidebar = (ROOT / "site/sidebars.js").read_text()
    overview = (ROOT / "site/docs/service-contracts.md").read_text()
    for service in reference.NAMES:
        for text in (sidebar, overview):
            assert f"pathname:///contracts/{service}/v1/'" in text or f"pathname:///contracts/{service}/v1/)" in text
    for kind in KINDS:
        assert f"pathname:///contracts/graph_engine/v1/resources/#{kind.lower()}'" in sidebar
        assert f"pathname:///contracts/graph_engine/v1/resources/{kind.lower()}.schema.yaml" in overview

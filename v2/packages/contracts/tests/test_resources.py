from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import reference
import resources
from resources import API_VERSION, GROUPS, KINDS, group_of, load_documents, validate_resource, validate_text

EXAMPLE_FILES = sorted(path for group in GROUPS for path in group.examples.glob("*.yaml"))


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


@pytest.mark.parametrize("group", GROUPS, ids=lambda group: group.service)
def test_checked_in_reference_is_current(group: resources.Group) -> None:
    path = resources.reference_path(group)
    assert path.exists() and path.read_text() == resources.page(group), (
        f"{group.service}/v1/resources/reference.html is stale: run `uv run python packages/contracts/tooling/resources.py`"
    )


@pytest.mark.parametrize("group", GROUPS, ids=lambda group: group.service)
def test_reference_documents_every_kind_and_example(group: resources.Group) -> None:
    page = resources.page(group)
    for kind in group.kinds:
        assert f'id="{kind.lower()}"' in page
        assert f"<code>kind: {kind}</code>" in page
    assert f"<code>apiVersion: {API_VERSION}</code>" in page


def test_every_service_contract_has_its_resources() -> None:
    assert {group.service for group in GROUPS} <= set(reference.NAMES)
    assert group_of("Connection").service == "connection_bus"
    assert group_of("ToolGrant").service == "tool_service"


def test_every_resource_uses_the_one_api_version() -> None:
    assert API_VERSION == "openengine.cc/v1alpha1"
    meta = resources._load(resources.META)["$defs"]
    assert [name for name in meta if "version" in name] == ["api_version"]
    for kind in KINDS:
        schema = resources._load(resources.schema_path(kind))
        assert schema["properties"]["apiVersion"] == {"$ref": "../../../common/v1/meta.schema.yaml#/$defs/api_version"}, kind
    for path in EXAMPLE_FILES:
        assert {document["apiVersion"] for document in load_documents(path.read_text())} == {API_VERSION}, path.name


# `apiVersion: x`, `"apiVersion": "x"` and the escaped `\"apiVersion\":\"x\"` inside a JSON string.
_API_VERSION_VALUE = re.compile(r"""apiVersion\\?["']?\s*:\s*\\?["']?([A-Za-z0-9./-]+)""")


def test_no_contract_file_names_another_api_version() -> None:
    contract_files = [
        path
        for path in resources.CONTRACTS.glob("*/v1/**/*")
        if path.suffix in {".yaml", ".json"} and "node_modules" not in path.parts
    ]
    found = {
        (path.relative_to(resources.CONTRACTS).as_posix(), value)
        for path in contract_files
        for value in _API_VERSION_VALUE.findall(path.read_text())
    }
    assert found, "the pattern no longer finds apiVersion values"
    assert {entry for entry in found if entry[1] != API_VERSION} == set()


def _problems(document: dict[str, Any]) -> list[str]:
    return [str(problem) for problem in validate_resource(document)]


def test_unknown_kind_and_version_are_refused() -> None:
    assert _problems({**_example("Graph"), "kind": "Widget"}) == [
        "kind: must be one of Connection, Graph, Loop, Tool, ToolGrant, ToolInvocation, Workspace"
    ]
    for version in ("openengine.cc/v1", "openengine.cc/v2alpha1", "tools.openengine.cc/v1alpha1"):
        assert _problems({**_example("Tool"), "apiVersion": version}) == ["apiVersion: must be openengine.cc/v1alpha1"]


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


def test_tool_schemas_are_objects_and_recovery_is_known() -> None:
    tool = _example("Tool")
    tool["spec"]["inputSchema"] = {"type": "string"}
    tool["spec"]["execution"]["recovery"] = "hope"
    assert _problems(tool) == [
        "spec.execution.recovery: 'hope' is not one of ['retry-safe', 'provider-idempotent', 'reconcile', 'manual']",
        "spec.inputSchema.type: 'object' was expected",
    ]


def test_tool_names_are_subject_tokens() -> None:
    for name in ("github.issue", "github_issue", "Github", "-github", "github-"):
        tool = _example("Tool")
        tool["metadata"]["name"] = name
        assert _problems(tool), name
    tool = _example("Tool")
    del tool["metadata"]["namespace"]
    assert _problems(tool) == ["metadata: 'namespace' is a required property"]


def test_a_grant_names_a_workspace() -> None:
    grant = _example("ToolGrant")
    del grant["spec"]["subject"]["workspaceRef"]
    assert _problems(grant) == ["spec.subject: 'workspaceRef' is a required property"]


def test_an_invocation_pins_its_tool() -> None:
    invocation = _example("ToolInvocation")
    del invocation["spec"]["toolRef"]["uid"]
    invocation["status"]["phase"] = "Done"
    assert _problems(invocation) == [
        "spec.toolRef: 'uid' is a required property",
        "status.phase: 'Done' is not one of ['Accepted', 'Running', 'Succeeded', 'Failed', 'BlockedAuth', 'OutcomeUnknown', 'Cancelled']",
    ]


def test_connection_scope_lists_resources_only_when_selected() -> None:
    connection = _example("Connection")
    connection["spec"]["scope"] = {"mode": "All", "resources": ["a/b"]}
    assert _problems(connection)
    connection["spec"]["scope"] = {"mode": "Selected"}
    assert _problems(connection)
    connection["spec"]["scope"] = {"mode": "All"}
    assert _problems(connection) == []


def test_problems_in_a_multi_document_file_name_the_document() -> None:
    text = "apiVersion: openengine.cc/v1alpha1\nkind: Graph\n---\nkind: Loop\n"
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
        service = group_of(kind).service
        assert f"pathname:///contracts/{service}/v1/resources/#{kind.lower()}'" in sidebar
        assert f"pathname:///contracts/{service}/v1/resources/{kind.lower()}.schema.yaml" in overview


@pytest.mark.parametrize("doc", ["v2/ARCHITECTURE.md", "v2/packages/contracts/README.md", "site/docs/service-contracts.md"])
def test_docs_name_only_the_one_api_version(doc: str) -> None:
    text = (ROOT / doc).read_text()
    assert set(re.findall(r"[a-z.]*openengine\.cc/v\d+(?:alpha|beta)\d+", text)) <= {API_VERSION}

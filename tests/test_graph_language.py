"""The YAML graph language: stage sections, the written order, and what `flow` overrides."""

from __future__ import annotations

import pytest
import yaml

from engine.cli import starters
from engine.graph_service.language import GraphError, parse_graph

SKETCH = """
apiVersion: openengine.cc/v1
kind: Graph
name: sketch

plan:
  workspace:
    base_ref: origin/main
  spec:
    agent: claude
    prompt: Spec ${instruction}
  tdd:
    agent: {same: spec}
    prompt: Write failing tests for ${outputs.spec}

implementation:
  implementer:
    agent: {same: spec}
    prompt: Implement ${outputs.spec}; findings so far ${json(outputs.reranker.findings)}
  ci_check:
    ci: true

review:
  reviewers:
    agent: {not: implementer}
    prompt: Review for ${item.facet.name}
    facets:
      - {facet: security, model: elevated}
      - {facet: bugs}
    outputs:
      findings: {type: findings, required: true}

  reranker:
    agent: {same: implementer}
    prompt: Consolidate ${json(outputs.reviewers)}
    outputs:
      findings: {type: findings, required: true, lineage: required}

flow:
  - from: reranker
    route:
      - {when: "size(outputs.reranker.findings) > 0 && visits.implementer < 2", to: implementer}
      - {to: end}
"""


def graph(text: str):
    return parse_graph(yaml.safe_load(text), runners=("claude", "codex"))


def test_steps_run_section_by_section_in_the_order_written() -> None:
    spec = graph(SKETCH)
    assert spec.checkout == "workspace" and spec.base_ref == "origin/main"
    assert [(node.id, node.stage, node.kind) for node in spec.nodes] == [
        ("spec", "plan", "agent"),
        ("tdd", "plan", "agent"),
        ("implementer", "implement", "agent"),
        ("ci_check", "implement", "ci"),
        ("reviewers", "review", "agent"),
        ("reranker", "review", "agent"),
    ]
    assert set(spec.edges()) == {
        ("start", "spec", False),
        ("spec", "tdd", False),
        ("tdd", "implementer", False),
        ("implementer", "ci_check", False),
        ("ci_check", "reviewers", False),
        ("reviewers", "reranker", False),
        ("reranker", "implementer", True),
        ("reranker", "end", True),
    }


def test_reviewers_fan_out_one_per_facet() -> None:
    reviewers = graph(SKETCH).node("reviewers")
    assert reviewers is not None
    assert reviewers.keys == ("reviewers-security", "reviewers-bugs")
    assert reviewers.parallel[0].item["facet"]["id"] == "security"
    assert reviewers.model == "${item.model}"


def test_a_custom_reviewer_names_its_facet() -> None:
    raw = yaml.safe_load(SKETCH)
    raw["review"]["reviewers"]["facets"].append(
        {"facet": {"id": "accessibility", "name": "Accessibility", "focus": "a11y"}},
    )
    reviewers = parse_graph(raw).node("reviewers")
    assert reviewers is not None
    assert reviewers.parallel[-1].item["facet"]["id"] == "accessibility"


def test_the_checkout_is_implicit_when_not_written() -> None:
    raw = yaml.safe_load(SKETCH)
    del raw["plan"]["workspace"]
    spec = parse_graph(raw)
    assert spec.checkout == "workspace" and spec.base_ref == ""
    assert spec.entries == ("spec",)


def test_a_step_given_a_way_out_in_flow_leaves_the_written_order() -> None:
    raw = yaml.safe_load(SKETCH)
    raw["flow"].append("tdd -> ci_check")
    raw["flow"].append("implementer -> end")
    edges = set(parse_graph(raw).edges())
    assert ("tdd", "ci_check", False) in edges and ("tdd", "implementer", False) not in edges


def test_every_starter_parses() -> None:
    for name in starters.NAMES:
        spec = parse_graph(yaml.safe_load(starters.source(name)))
        assert spec.node("reviewers") is not None and spec.node("reranker") is not None


def test_adversarial_review_checks_out_the_branch_and_runs_the_agent_input() -> None:
    spec = parse_graph(yaml.safe_load(starters.source("adversarial-review")))
    assert spec.ref_input == "branch"
    assert {item.name for item in spec.inputs} == {"branch", "agent"}
    assert spec.node("reviewers").runner.kind == spec.node("reranker").runner.kind == "input"


def test_mistakes_are_reported_with_their_section() -> None:
    raw = yaml.safe_load(SKETCH)
    raw["implementation"]["setup"] = {"base_ref": "origin/main"}
    raw["review"]["reviewers"]["facets"].append({"facet": "typo"})
    raw["review"]["reranker"]["stage"] = "review"
    raw["nodes"] = {}
    with pytest.raises(GraphError) as raised:
        parse_graph(raw)
    found = {problem.path: problem.message for problem in raised.value.problems}
    assert "must be the first step" in found["implementation.setup"]
    assert "unknown facet 'typo'" in found["review.reviewers.facets[2].facet"]
    assert "unknown field" in found["review.reranker.stage"]
    assert "unknown field" in found["nodes"]


def test_workspace_names_only_the_checkout() -> None:
    raw = yaml.safe_load(SKETCH)
    del raw["plan"]["workspace"]
    raw["plan"]["workspace"] = {"agent": "claude", "prompt": "x"}
    with pytest.raises(GraphError) as raised:
        parse_graph(raw)
    assert any(problem.path == "plan.workspace" for problem in raised.value.problems)


def test_a_step_name_is_used_once_across_sections() -> None:
    raw = yaml.safe_load(SKETCH)
    raw["review"]["spec"] = {"agent": "claude", "prompt": "again"}
    with pytest.raises(GraphError) as raised:
        parse_graph(raw)
    assert any("already a step in plan" in problem.message for problem in raised.value.problems)


def test_an_output_must_be_able_to_have_run_first() -> None:
    raw = yaml.safe_load(SKETCH)
    raw["plan"]["spec"]["prompt"] = "Spec ${outputs.tdd}"
    with pytest.raises(GraphError) as raised:
        parse_graph(raw)
    assert any(
        problem.path == "plan.spec.prompt" and "cannot have run before spec" in problem.message
        for problem in raised.value.problems
    )


def test_the_sketch_compiles_to_langgraph(tmp_path) -> None:
    import sys

    from langgraph_acp import ACPAgentRegistry, StdioACPProvider

    from engine.graph_service.compile import compile_graph

    registry = ACPAgentRegistry([
        StdioACPProvider(name=name, command=(sys.executable, "-c", "")) for name in ("claude", "codex")
    ])
    workflow = compile_graph(
        graph(SKETCH), version_id="gv-sketch", number=1, workspace_provider=object(), registry=registry,
    )
    drawn = workflow.builder.compile().get_graph()
    assert {"workspace", "spec", "tdd", "implementer", "ci_check", "reviewers-security",
            "reviewers-bugs", "reranker"} <= set(drawn.nodes)
    edges = {(edge.source, edge.target) for edge in drawn.edges}
    assert {("__start__", "workspace"), ("workspace", "spec"), ("ci_check", "reviewers-bugs"),
            ("reranker", "implementer"), ("reranker", "__end__")} <= edges


@pytest.mark.parametrize(("field", "value", "path"), [
    ("inputs", [], "inputs"),
    ("inputs", {"tone": "bad"}, "inputs.tone"),
    ("inputs", {"bad-name": {}}, "inputs.bad-name"),
    ("inputs", {"tone": {"required": "yes"}}, "inputs.tone.required"),
    ("inputs", {"tone": {"default": []}}, "inputs.tone.default"),
    ("inputs", {"tone": {"choices": "plain"}}, "inputs.tone.choices"),
    ("inputs", {"tone": {"default": "x", "choices": ["y"]}}, "inputs.tone.default"),
    ("loop", "hourly", "loop"),
    ("loop", {"instruction": [1]}, "loop.instruction"),
    ("flow", {}, "flow"),
    ("flow", ["not an edge"], "flow[0]"),
    ("flow", [42], "flow[0]"),
    ("flow", [{"to": "end"}], "flow[0].from"),
    ("flow", [{"from": "work"}], "flow[0]"),
    ("flow", [{"from": "start", "route": []}], "flow[0].from"),
    ("flow", [{"from": "work", "route": []}], "flow[0].route"),
    ("flow", [{"from": "work", "route": [42]}], "flow[0].route[0]"),
    ("flow", [{"from": "work", "route": [{"when": 42, "to": "end"}]}], "flow[0].route[0].when"),
    ("flow", [{"from": "work", "route": [{"when": "true", "to": "end"}]}], "flow[0].route"),
    ("flow", [{"from": "work", "route": [{"to": "end"}, {"to": "end"}]}], "flow[0].route"),
])
def test_invalid_graph_fields_report_their_location(field, value, path):
    raw = {
        "apiVersion": "openengine.cc/v1", "name": "validation",
        "implementation": {"work": {"agent": "claude", "prompt": "go"}},
        field: value,
    }
    with pytest.raises(GraphError) as raised:
        parse_graph(raw)
    assert path in {problem.path for problem in raised.value.problems}


@pytest.mark.parametrize(("fields", "path"), [
    ({"agent": None}, "agent"),
    ({"agent": {}}, "agent"),
    ({"agent": {"same": "missing"}}, "agent.same"),
    ({"agent": "${outputs.work}"}, "agent"),
    ({"agent": "${inputs.missing}"}, "agent"),
    ({"prompt": ""}, "prompt"),
    ({"tools": "shell"}, "tools"),
    ({"tools": ["unknown"]}, "tools[0]"),
    ({"model": 12}, "model"),
    ({"steering": "sometimes"}, "steering"),
    ({"outputs": []}, "outputs"),
    ({"outputs": {"bad-name": {}}}, "outputs.bad-name"),
    ({"outputs": {"result": "string"}}, "outputs.result"),
    ({"outputs": {"result": {"enum": 42}}}, "outputs.result.enum"),
    ({"outputs": {"result": {"type": "unknown"}}}, "outputs.result.type"),
    ({"outputs": {"result": {"type": "number", "enum": ["x"]}}}, "outputs.result.enum"),
    ({"outputs": {"result": {"lineage": True}}}, "outputs.result.lineage"),
    ({"outputs": {"result": {"required": "yes"}}}, "outputs.result.required"),
    ({"facets": {}}, "facets"),
    ({"facets": ["security"]}, "facets[0]"),
    ({"facets": [{"facet": {}}]}, "facets[0].facet"),
    ({"facets": [{"facet": 42}]}, "facets[0].facet"),
    ({"facets": [{"facet": {"id": "bad id", "name": "name", "focus": "focus"}}]}, "facets[0].facet.id"),
    ({"facets": [{"facet": "security"}, {"facet": "security"}]}, "facets"),
])
def test_invalid_agent_settings_report_their_location(fields, path):
    raw = {
        "apiVersion": "openengine.cc/v1", "name": "validation",
        "implementation": {"work": {"agent": "claude", "prompt": "go", **fields}},
    }
    with pytest.raises(GraphError) as raised:
        parse_graph(raw)
    assert f"implementation.work.{path}" in {problem.path for problem in raised.value.problems}

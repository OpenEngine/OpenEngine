"""The YAML graph language: stage sections, the written order, and what `flow` overrides."""

from __future__ import annotations

import pytest
import yaml

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


def test_graph_digest_includes_api_version() -> None:
    from dataclasses import replace
    spec = parse_graph(yaml.safe_load(SKETCH))
    assert spec.json()["apiVersion"] == "openengine.cc/v1"
    assert replace(spec, api_version="openengine.dev/v1").digest != spec.digest


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
    # Without `naming:` a run keeps its fallback name: no naming turn is compiled.
    assert "naming" not in drawn.nodes


NAMED = """
apiVersion: openengine.cc/v1
kind: Graph
name: named
inputs:
  runner: {default: claude, choices: [claude, codex, qwen]}
naming: NAMING
implementation:
  work:
    agent: ${inputs.runner}
    prompt: Do ${instruction}
"""


def _compiled_naming(naming: str, **options):
    import sys

    from langgraph_acp import ACPAgentRegistry, StdioACPProvider
    from langgraph_acp.providers import ClaudeACPProvider

    from engine.graph_service.compile import compile_graph

    registry = ACPAgentRegistry([
        ClaudeACPProvider(command=(sys.executable, "-c", "")),
        *(StdioACPProvider(name=name, command=(sys.executable, "-c", "")) for name in ("codex", "qwen")),
    ])
    spec = parse_graph(yaml.safe_load(NAMED.replace("NAMING", naming)), runners=("claude", "codex", "qwen"))
    return compile_graph(
        spec, version_id="gv-named", number=1, workspace_provider=object(), registry=registry, **options,
    ).builder


def test_naming_adds_a_hidden_name_node_between_the_checkout_and_the_first_step() -> None:
    from engine.graph_service.compile import GraphNameNode

    builder = _compiled_naming("{agent: \"${inputs.runner}\"}")
    edges = {(edge.source, edge.target) for edge in builder.compile().get_graph().edges}
    assert {("__start__", "workspace"), ("workspace", "naming"), ("naming", "work")} <= edges
    assert ("workspace", "work") not in edges
    node = builder.nodes["naming"].runnable.afunc
    # A NameNode: it writes the run's `name`, stays out of the sidebar and cannot fail the run.
    assert isinstance(node, GraphNameNode)
    assert node.output_key == "name" and node.graph_node_show_in_sidebar is False
    # It reads issue text anyone can write, so it may not act on any of it.
    assert node.refuses_permissions is True


@pytest.mark.parametrize(("naming", "options", "runner", "model"), [
    ("{agent: claude}", {}, "claude", "haiku"),
    ("{agent: codex}", {}, "codex", None),
    # An added agent names on its own model.
    ("{agent: qwen}", {"agent_models": {"qwen": "engine/qwen3"}}, "qwen", "engine/qwen3"),
    ("{agent: claude, model: sonnet}", {}, "claude", "sonnet"),
    ("{agent: claude, model: default}", {}, "claude", "sonnet"),
    # A backend's tiers replace the defaults it names and keep the rest.
    ("{agent: claude}", {"model_tiers": {"claude": {"default": "opus"}}}, "claude", "haiku"),
    ("{agent: claude}", {"model_tiers": {"claude": {"cheapest": "claude-haiku-9"}}}, "claude", "claude-haiku-9"),
])
def test_naming_runs_on_the_cheapest_model_unless_told_otherwise(naming, options, runner, model) -> None:
    node = _compiled_naming(naming, **options).nodes["naming"].runnable.afunc
    chosen = node._for_runner(runner)
    assert chosen.agent == runner and (chosen.session_config or {}).get("model") == model


def test_naming_on_claude_only_reads_whatever_mode_the_operator_set() -> None:
    from engine.graph_service.compile import NAMING_TOOLS

    builder = _compiled_naming(
        "{agent: \"${inputs.runner}\"}",
        session_config={"claudeCode": {"options": {"outputStyle": "terse"}}},
    )
    node = builder.nodes["naming"].runnable.afunc
    claude = node._for_runner("claude").session_config
    assert claude["mode"] == "default"
    assert claude["claudeCode"]["options"] == {
        "outputStyle": "terse", "allowedTools": list(NAMING_TOOLS), "tools": list(NAMING_TOOLS),
    }
    # The pin is Claude's own config; another runner's session is left as it was.
    assert "mode" not in node._for_runner("codex").session_config


def test_naming_follows_the_runner_input_a_run_chooses(monkeypatch) -> None:
    import asyncio

    from engine.graph_runtime_langgraph.components import NameNode

    node = _compiled_naming("{agent: \"${inputs.runner}\"}").nodes["naming"].runnable.afunc
    seen = []

    async def named(self, state):
        seen.append(self)
        return {"name": "Fix it"}

    monkeypatch.setattr(NameNode, "__call__", named)
    assert asyncio.run(node({"inputs": {"runner": "codex"}})) == {"name": "Fix it"}
    chosen = seen[0]._for_runner(seen[0].agent)
    assert chosen.agent == "codex" and not (chosen.session_config or {}).get("model")


@pytest.mark.parametrize(("naming", "path"), [
    ("claude", "naming"),
    ({}, "naming.agent"),
    ({"agent": {"same": "work"}}, "naming.agent"),
    ({"agent": "${inputs.missing}"}, "naming.agent"),
    ({"agent": "absent"}, "naming.agent"),
    ({"agent": "claude", "model": 12}, "naming.model"),
    ({"agent": "claude", "model": "${outputs.work}"}, "naming.model"),
    ({"agent": "claude", "prompt": "name it"}, "naming.prompt"),
])
def test_invalid_naming_reports_its_location(naming, path) -> None:
    raw = {
        "apiVersion": "openengine.cc/v1", "name": "validation", "naming": naming,
        "implementation": {"work": {"agent": "claude", "prompt": "go"}},
    }
    with pytest.raises(GraphError) as raised:
        parse_graph(raw, runners=("claude",))
    assert path in {problem.path for problem in raised.value.problems}


def test_naming_reserves_its_step_name() -> None:
    raw = {
        "apiVersion": "openengine.cc/v1", "name": "validation", "naming": {"agent": "claude"},
        "implementation": {"naming": {"agent": "claude", "prompt": "go"}},
    }
    with pytest.raises(GraphError, match="'naming' is used twice"):
        parse_graph(raw)
    del raw["naming"]
    assert parse_graph(raw).naming is None


def test_tdd_guide_writes_tests_first_and_bounds_its_retries() -> None:
    from pathlib import Path

    from engine.graph_service.expressions import parse

    source = Path(__file__).parents[1] / "docs/examples/graphs/tdd-guide.yaml"
    spec = graph(source.read_text())
    assert [node.id for node in spec.nodes] == [
        "pick", "conventions", "tdd", "implement", "publish", "ci", "reviewers", "reproduce", "reranker", "impact", "human_review",
    ]
    # A run can build on an unmerged branch instead of main, and publishes against it.
    assert (spec.base_ref, spec.ref_input) == ("origin/main", "base")
    # Runs are named by the issue picker's runner, on its cheapest model.
    assert spec.naming is not None and (spec.naming.runner.kind, spec.naming.runner.value, spec.naming.model) == (
        "input", "runner", "")
    assert {
        ("tdd", "implement", False),
        ("implement", "publish", False),
        ("publish", "ci", False),
        ("reviewers", "reproduce", False),
        ("reproduce", "reranker", False),
        ("impact", "human_review", False),
    } <= set(spec.edges())
    # The house review panel, with how the guides read and whether the docs build as extra angles.
    assert spec.node("reviewers").keys == tuple(
        f"reviewers-{facet}" for facet in ("security", "bugs", "performance", "conciseness", "dryness", "guide", "docs-build")
    )
    # Who writes the tests, who implements and who reviews are each the run's
    # choice, so a run can use one runner throughout or a different one for each.
    assert [(node.id, node.runner.kind, node.runner.value) for node in spec.nodes if node.kind == "agent"] == [
        ("pick", "input", "runner"),
        ("conventions", "same", "pick"),
        ("tdd", "same", "pick"),
        ("implement", "input", "implementer"),
        ("publish", "same", "implement"),
        ("reviewers", "input", "reviewer"),
        ("reproduce", "input", "reviewer"),
        ("reranker", "input", "reranker"),
        ("impact", "input", "reviewer"),
    ]
    # Only publish opens the pull request; CI and the review find it by the URL it reports.
    assert [node.id for node in spec.nodes if "open_pull_request" in node.tools] == ["publish"]
    assert [output.name for output in spec.node("publish").outputs] == ["pr_url"]
    # The reranker's findings and the impact rating are posted on the pull
    # request, and the implementer answers the threads it acts on.
    assert [node.id for node in spec.nodes if "add_comment" in node.tools] == ["implement", "reranker", "impact"]
    # A comment on the pull request re-enters the run at the implementer.
    assert [node.id for node in spec.nodes if node.always_open] == ["implement"]

    routes = {rule.sources[0]: rule.routes for rule in spec.flow if rule.routes}

    def after(step: str, outputs: dict, visits: dict | None = None, inputs: dict | None = None) -> str:
        names = {"outputs": outputs, "visits": visits or {}, "inputs": inputs or {}, "instruction": "", "item": None}
        return next(route.targets[0] for route in routes[step] if not route.when or parse(route.when).evaluate(names))

    assert after("pick", {"pick": {"found": "no"}}) == "end"
    assert after("pick", {"pick": {"found": "yes"}}) == "conventions"
    # The conventions are in place before any work; ready-made tests skip the test writer.
    assert after("conventions", {}, inputs={"tests_ready": "no"}) == "tdd"
    assert after("conventions", {}, inputs={"tests_ready": "yes"}) == "implement"
    finding = {"tagline": "x", "description": "y", "agent": "claude", "facet": "bugs"}
    assert after("reranker", {"reranker": {"findings": [finding]}}, {"implement": 1}) == "implement"
    assert after("reranker", {"reranker": {"findings": [finding]}}, {"implement": 3}) == "impact"
    assert after("reranker", {"reranker": {"findings": []}}, {"implement": 1}) == "impact"
    assert after("ci", {"ci": {"passed": False}}, {"implement": 1}) == "implement"
    # The panel reviews once CI is done; a person has the last word after
    # impact analysis, and can send the change back with a note.
    assert after("ci", {"ci": {"passed": False}}, {"implement": 4}) == "reviewers"
    assert after("ci", {"ci": {"passed": True}}, {"implement": 1}) == "reviewers"
    approved = {"decision": "approved", "note": ""}
    assert after("human_review", {"human_review": approved}, {"human_review": 1}) == "end"
    sent_back = {"decision": "rejected", "note": "Name the flag --check, not --verify."}
    assert after("human_review", {"human_review": sent_back}, {"human_review": 1}) == "implement"
    assert after("human_review", {"human_review": sent_back}, {"human_review": 3}) == "end"
    assert after("human_review", {"human_review": {"decision": "rejected", "note": ""}}, {"human_review": 1}) == "end"


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

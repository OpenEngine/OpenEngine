"""`oe graph validate` and `oe graph spec`: offline, from the Graph schema and rules alone."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
import yaml
from openengine.cli.__main__ import main

EXAMPLES = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "graph_engine" / "v1" / "resources" / "examples"
GRAPH = EXAMPLES / "implement-review.graph.yaml"
NOTE = "note: runners and tools are not checked here; they are checked when a run starts"
MALFORMED = """\
apiVersion: openengine.cc/v1alpha1
kind: Graph
metadata: {name: Bad_Name}
spec:
  plan:
    - name: a
      agent: {runner: {name: claude}}
      human: {prompt: x}
  flow:
    - {from: []}
"""


def _validate(capsys: pytest.CaptureFixture[str], target: str) -> tuple[int, list[str]]:
    code = main(["graph", "validate", target])
    return code, capsys.readouterr().out.splitlines()


def test_a_valid_graph_is_named(capsys: pytest.CaptureFixture[str]) -> None:
    assert _validate(capsys, str(GRAPH)) == (0, [f"{GRAPH}: valid Graph implement-review", NOTE])


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*.graph.yaml")), ids=lambda path: path.name)
def test_the_examples_are_valid_offline(capsys: pytest.CaptureFixture[str], example: Path) -> None:
    code, lines = _validate(capsys, str(example))
    assert code == 0 and lines[-1] == NOTE


def test_every_independent_problem_is_reported_with_its_path(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    path = tmp_path / "bad.graph.yaml"
    path.write_text(MALFORMED)
    code, lines = _validate(capsys, str(path))
    assert code == 1
    assert [line.split(": ")[1] for line in lines] == [
        "metadata.name", "spec.flow[0]", "spec.flow[0].from", "spec.plan[0]", "spec.plan[0].agent",
    ]
    assert all(line.startswith(f"{path}: ") for line in lines)
    assert f"{path}: spec.plan[0]: set exactly one of agent, human, ci" in lines


def test_standard_input_is_read_for_a_dash(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(GRAPH.read_text()))
    assert _validate(capsys, "-") == (0, ["-: valid Graph implement-review", NOTE])
    monkeypatch.setattr("sys.stdin", io.StringIO(MALFORMED))
    code, lines = _validate(capsys, "-")
    assert code == 1 and len(lines) == 5 and all(line.startswith("-: ") for line in lines)


def test_every_graph_in_a_file_is_named(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    graph = yaml.safe_load(GRAPH.read_text())
    other = {**graph, "metadata": {"name": "other"}}
    path = tmp_path / "two.yaml"
    path.write_text(yaml.safe_dump_all([graph, other]))
    assert _validate(capsys, str(path)) == (
        0, [f"{path}: valid Graph implement-review", f"{path}: valid Graph other", NOTE],
    )


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ((EXAMPLES / "weekday-issues.loop.yaml").read_text(), "kind: is a Loop, not a Graph"),
        ("apiVersion: openengine.cc/v1\nkind: Graph\nmetadata: {name: g}\nspec: {}\n",
         "apiVersion: must be openengine.cc/v1alpha1; openengine.cc/v1 is a V1 manifest, which is not accepted"),
        ("apiVersion: openengine.cc/v1alpha1\nmetadata: {name: g}\n", "kind: must be Graph"),
        ("a: [", "(document): not YAML"),
        ("", "(document): no resources"),
    ],
    ids=["loop", "v1-manifest", "no-kind", "not-yaml", "empty"],
)
def test_anything_but_a_graph_is_refused(capsys: pytest.CaptureFixture[str], tmp_path: Path, text: str, problem: str) -> None:
    path = tmp_path / "resource.yaml"
    path.write_text(text)
    code, lines = _validate(capsys, str(path))
    assert code == 1
    assert lines[0].startswith(f"{path}: {problem}")


def test_a_missing_file_is_a_problem(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    code, lines = _validate(capsys, str(tmp_path / "missing.yaml"))
    assert code == 1 and lines[0].startswith(f"{tmp_path / 'missing.yaml'}: cannot read:")


def test_spec_prints_the_graph_reference_as_markdown(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["graph", "spec"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Graph\n")
    for title in ("GraphSpec", "Step", "AgentStep", "HumanStep", "CIStep", "Runner", "FlowRule", "Route"):
        assert f"\n## {title}\n" in out
    assert "Set exactly one of `agent`, `human`, `ci`." in out
    assert "| `prompt` (required) | `string` |" in out


@pytest.mark.parametrize("version", [None, "openengine.cc/v1", "unknown"])
@pytest.mark.parametrize("plan", [None, "not an array"])
def test_bad_version_keeps_independent_schema_errors(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    version: str | None, plan: str | None,
) -> None:
    document = yaml.safe_load(MALFORMED)
    if version is None:
        del document["apiVersion"]
    else:
        document["apiVersion"] = version
    document["unexpected"] = True
    if plan is not None:
        document["spec"]["plan"] = plan
    original = yaml.safe_dump(document)
    monkeypatch.setattr("sys.stdin", io.StringIO(original))
    code, lines = _validate(capsys, "-")
    assert code == 1
    paths = [line.split(": ")[1] for line in lines]
    assert paths.count("apiVersion") == 1
    assert {"(document)", "metadata.name", "spec.flow[0]", "spec.flow[0].from"} <= set(paths)
    if plan is None:
        assert {"spec.plan[0]", "spec.plan[0].agent"} <= set(paths)
    else:
        assert "spec.plan" in paths
    assert yaml.safe_dump(document) == original


def test_spec_prints_the_expression_syntax(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["graph", "spec"]) == 0
    section = capsys.readouterr().out.split("\n## Expression syntax\n", 1)[1]
    for text in ("`inputs`", "`outputs`", "`visits`", "`item`", "`&&`", "`||`", "`!`", "`in`",
                 "`size(...)`", "`json(...)`", "`lower(...)`", "`has(...)`", "Examples:"):
        assert text in section


def _bad_example() -> str:
    """The example with a duplicate step, a flow to `nowhere`, a plain `a -> b -> a` loop and `${inputs.missing}`."""
    graph = yaml.safe_load(GRAPH.read_text())
    spec = graph["spec"]
    spec["plan"] = [
        {"name": "a", "human": {"prompt": "Plan ${inputs.missing}"}},
        {"name": "b", "human": {"prompt": "Check the plan"}},
    ]
    spec["review"].append({"name": "approve", "human": {"prompt": "Approve again"}})
    spec["flow"][:0] = [
        {"from": ["a"], "to": ["b"]},
        {"from": ["b"], "to": ["a", "implement"]},
        {"from": ["publish"], "to": ["ci", "nowhere"]},
    ]
    return yaml.safe_dump(graph, sort_keys=False)


def test_independent_graph_errors_are_all_reported(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    path = tmp_path / "bad.graph.yaml"
    path.write_text(_bad_example())
    assert _validate(capsys, str(path)) == (1, [
        f"{path}: spec.review[3].name: duplicate step 'approve'; spec.review[2] has the same name",
        f"{path}: spec.flow[2].to: unknown step 'nowhere'",
        f"{path}: spec.flow: cycle a -> b -> a does not pass through a route",
        f"{path}: spec.plan[0].human.prompt: undeclared input 'missing'",
    ])


def test_schema_and_graph_errors_are_reported_together(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    graph = yaml.safe_load(_bad_example())
    graph["metadata"]["name"] = "Bad_Name"
    path = tmp_path / "bad.graph.yaml"
    path.write_text(yaml.safe_dump(graph))
    code, lines = _validate(capsys, str(path))
    assert code == 1
    assert [line.split(": ")[1] for line in lines] == [
        "metadata.name", "spec.review[3].name", "spec.flow[2].to", "spec.flow", "spec.plan[0].human.prompt",
    ]


def _graph(**spec: Any) -> dict[str, Any]:
    """A valid graph, `a` then `b`, with an input `topic`, changed by `spec`."""
    return {
        "apiVersion": "openengine.cc/v1alpha1", "kind": "Graph", "metadata": {"name": "g"},
        "spec": {
            "inputs": [{"name": "topic"}],
            "plan": [
                {"name": "a", "agent": {"runner": {"name": "claude"}, "prompt": "Plan ${inputs.topic}"}},
                {"name": "b", "human": {"prompt": "Approve ${outputs.a}"}},
            ],
            **spec,
        },
    }


def _steps(*steps: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": name, "human": {"prompt": "ok"}} if isinstance(name, str) else name for name in steps]


def _agent(prompt: str, **agent: Any) -> dict[str, Any]:
    return {"name": "a", "agent": {"runner": {"name": "claude"}, "prompt": prompt, **agent}}


ROUTE_BACK = {"from": ["b"], "route": [{"when": "visits.a < 3", "to": "a"}, {"to": "end"}]}


@pytest.mark.parametrize(
    ("graph", "problems"),
    [
        (_graph(plan=_steps("a", "end")), ["spec.plan[1].name: 'end' is reserved"]),
        (_graph(plan=_steps("start"), review=_steps("naming")),
         ["spec.plan[0].name: 'start' is reserved", "spec.review[0].name: 'naming' is reserved"]),
        (_graph(plan=_steps("a"), review=_steps("a")),
         ["spec.review[0].name: duplicate step 'a'; spec.plan[0] has the same name"]),
        (_graph(flow=[{"from": ["ghost"], "to": ["b"]}]), ["spec.flow[0].from: unknown step 'ghost'"]),
        (_graph(flow=[{"from": ["a"], "route": [{"when": "true", "to": "b"}, {"to": "ghost"}]}]),
         ["spec.flow[0].route[1].to: unknown step 'ghost'"]),
        (_graph(plan=_steps("a", "b"), flow=[{"from": ["a"], "to": ["start"]}]),
         ["spec.flow[0].to: start can only be a source", "spec.plan[1]: step 'b' is never reached from start"]),
        (_graph(flow=[{"from": ["start"], "to": ["b"]}]), ["spec.plan[0]: step 'a' is never reached from start"]),
        (_graph(flow=[{"from": ["b"], "route": [{"when": "visits.a < 3", "to": "a"}, {"to": "a"}]}]),
         ["spec.plan[0]: step 'a' never reaches end", "spec.plan[1]: step 'b' never reaches end"]),
        (_graph(flow=[{"from": ["b"], "to": ["a"]}]),
         ["spec.plan[0]: step 'a' never reaches end", "spec.plan[1]: step 'b' never reaches end",
          "spec.flow: cycle a -> b -> a does not pass through a route"]),
        (_graph(flow=[{"from": ["b"], "to": ["a", "end"]}]), ["spec.flow: cycle a -> b -> a does not pass through a route"]),
        (_graph(flow=[ROUTE_BACK, ROUTE_BACK]), ["spec.flow[1]: step 'b' has more than one route; merge them into one"]),
        (_graph(flow=[ROUTE_BACK, {"from": ["b"], "to": ["end"]}]),
         ["spec.flow[1]: step 'b' has both a route and plain edges out; use only the route"]),
        (_graph(flow=[{"from": ["b"], "route": [{"to": "a"}, {"when": "true", "to": "end"}]}]),
         ["spec.flow[0].route[1].when: the last branch is the default, which has no when",
          "spec.flow[0].route[0]: only the last branch can leave out when"]),
    ],
    ids=["reserved", "reserved-start-naming", "duplicate", "unknown-from", "unknown-route-target", "start-as-target",
         "unreachable", "never-ends", "plain-cycle", "plain-cycle-with-exit", "two-routes", "route-and-edges",
         "route-default"],
)
def test_structure_problems(capsys: pytest.CaptureFixture[str], tmp_path: Path, graph: dict[str, Any], problems: list[str]) -> None:
    path = tmp_path / "g.yaml"
    path.write_text(yaml.safe_dump(graph))
    assert _validate(capsys, str(path)) == (1, [f"{path}: {problem}" for problem in problems])


@pytest.mark.parametrize(
    ("graph", "problems"),
    [
        (_graph(plan=[_agent("${outputs.a.x ==}"), *_steps("b")]),
         ["spec.plan[0].agent.prompt: 'outputs.a.x ==': cannot parse: invalid syntax"]),
        (_graph(flow=[{"from": ["b"], "route": [{"when": "visits.a < 3 and true", "to": "a"}, {"to": "end"}]}]),
         ["spec.flow[0].route[0].when: 'visits.a < 3 and true': use && || ! rather than Python's 'and'"]),
        (_graph(plan=[_agent("${__import__('os')}"), *_steps("b")]),
         ["spec.plan[0].agent.prompt: \"__import__('os')\": only has, json, lower, size can be called"]),
        (_graph(plan=[_agent("${print(inputs.topic)}"), *_steps("b")]),
         ["spec.plan[0].agent.prompt: 'print(inputs.topic)': only has, json, lower, size can be called"]),
        (_graph(plan=[_agent("${repo.name}"), *_steps("b")]),
         ["spec.plan[0].agent.prompt: 'repo.name': unknown name 'repo'; "
          "use instruction, repository, inputs, outputs, visits or item"]),
        (_graph(plan=[_agent("${[x for x in inputs]}"), *_steps("b")]),
         ["spec.plan[0].agent.prompt: '[x for x in inputs]': use && || ! rather than Python's 'for'"]),
        (_graph(plan=[_agent("${size}"), *_steps("b")]), ["spec.plan[0].agent.prompt: 'size': size is a function"]),
        (_graph(plan=[_agent("${inputs.missing} ${inputs.missing}"), *_steps("b")]),
         ["spec.plan[0].agent.prompt: undeclared input 'missing'"]),
        (_graph(plan=[_agent("${outputs.ghost} ${visits.ghost}"), *_steps("b")]),
         ["spec.plan[0].agent.prompt: unknown step 'ghost' in outputs.ghost",
          "spec.plan[0].agent.prompt: unknown step 'ghost' in visits.ghost"]),
        (_graph(plan=[_agent("${outputs.b}"), *_steps("b")]),
         ["spec.plan[0].agent.prompt: outputs.b cannot have run before a"]),
        (_graph(plan=[_agent("${item.facet.name}"), *_steps("b")]),
         ["spec.plan[0].agent.prompt: item is only defined in a step with facets"]),
        (_graph(workspace={"ref": "${outputs.a}"}), ["spec.workspace.ref: outputs.a cannot have run before start"]),
        (_graph(plan=[*_steps("a"), {"name": "b", "human": {"prompt": "ok", "choose": "item.findings"}}]),
         ["spec.plan[1].human.choose: item is only defined in a step with facets"]),
        (_graph(plan=[_agent("ok"), {"name": "b", "agent": {"runner": {"sameAs": "ghost"}, "prompt": "ok"}}]),
         ["spec.plan[1].agent.runner.sameAs: unknown step 'ghost'"]),
        (_graph(plan=[_agent("${inputs}"), *_steps("b")]), ["spec.plan[0].agent.prompt: inputs needs a name, such as inputs.NAME"]),
    ],
    ids=["syntax", "python-and", "dunder", "other-call", "unknown-root", "comprehension", "bare-function", "undeclared-input",
         "unknown-step", "output-not-yet-run", "item-outside-facets", "output-in-workspace", "item-in-choose",
         "unknown-same-as", "bare-root"],
)
def test_expression_problems(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, graph: dict[str, Any], problems: list[str],
) -> None:
    path = tmp_path / "g.yaml"
    path.write_text(yaml.safe_dump(graph))
    assert _validate(capsys, str(path)) == (1, [f"{path}: {problem}" for problem in problems])


@pytest.mark.parametrize(
    "graph",
    [
        _graph(),
        _graph(flow=[ROUTE_BACK]),
        _graph(plan=[_agent("${item.facet.name} ${json(outputs.a)}", facets=[{"name": "bugs"}]), *_steps("b")]),
        _graph(plan=[_agent("${!(has(inputs.topic)) || lower(inputs.topic) in ['x', 'y'] && size(outputs.b[0]) % 2 == 1}"),
                     *_steps("b")], flow=[ROUTE_BACK]),
    ],
    ids=["written-order", "loop-through-route", "item-in-facets", "every-operator"],
)
def test_valid_graphs_pass(capsys: pytest.CaptureFixture[str], tmp_path: Path, graph: dict[str, Any]) -> None:
    path = tmp_path / "g.yaml"
    path.write_text(yaml.safe_dump(graph))
    assert _validate(capsys, str(path)) == (0, [f"{path}: valid Graph g", NOTE])

"""`oe graph validate` and `oe graph spec`: offline, from the Graph schema alone."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml
from openengine.cli.__main__ import main

EXAMPLES = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "graph_engine" / "v1" / "resources" / "examples"
GRAPH = EXAMPLES / "implement-review.graph.yaml"
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
    assert _validate(capsys, str(GRAPH)) == (0, [f"{GRAPH}: valid Graph implement-review"])


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
    assert _validate(capsys, "-") == (0, ["-: valid Graph implement-review"])
    monkeypatch.setattr("sys.stdin", io.StringIO(MALFORMED))
    code, lines = _validate(capsys, "-")
    assert code == 1 and len(lines) == 5 and all(line.startswith("-: ") for line in lines)


def test_every_graph_in_a_file_is_named(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    graph = yaml.safe_load(GRAPH.read_text())
    other = {**graph, "metadata": {"name": "other"}}
    path = tmp_path / "two.yaml"
    path.write_text(yaml.safe_dump_all([graph, other]))
    assert _validate(capsys, str(path)) == (0, [f"{path}: valid Graph implement-review", f"{path}: valid Graph other"])


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

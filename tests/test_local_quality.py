"""The local gate is fail closed and repairs before review without a model."""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from langgraph.graph import END, START, StateGraph

from engine.graph_runtime_langgraph import State
from engine.graph_runtime_langgraph.components.quality import QualityGate, QualityRetriesExhausted, after_quality
from engine.graph_runtime_langgraph.quality import run_checks


def configure(root, command, files=None):
    (root / "scan.py").write_text("pass\n")
    (root / ".quality.toml").write_text(
        'version = 1\n[[checks]]\nid = "example"\ncommand = ' + json.dumps(command)
        + '\nfiles = ' + json.dumps(files or ["scan.py"]) + '\n')


@pytest.mark.parametrize("command,files", [
    (["nonexistent-quality-executable"], None),
    ([sys.executable, "-c", "pass"], ["missing/**/*.py"]),
    ([sys.executable, "-c", "raise SystemExit(5)"], None),
])
def test_required_checks_fail_closed(tmp_path, command, files):
    configure(tmp_path, command, files)
    result = run_checks(tmp_path)
    assert result["passed"] is False
    diagnostic = result["diagnostics"][0]
    assert set(diagnostic) == {"id", "file", "line", "message", "command"}
    assert (Path(result["report"]) / "result.json").is_file()


def test_missing_or_empty_config_fails(tmp_path):
    assert not run_checks(tmp_path)["passed"]
    (tmp_path / ".quality.toml").write_text("version = 1\nchecks = []\n")
    assert not run_checks(tmp_path)["passed"]


@pytest.mark.parametrize("repair", [False, True])
def test_gate_routes_repairs_before_review_without_models(tmp_path, repair):
    configure(tmp_path, [sys.executable, "scan.py"])
    (tmp_path / "scan.py").write_text("raise SystemExit(1)\n")
    visits = []

    def implement(state):
        visits.append("implementation")
        if repair:
            (tmp_path / "scan.py").write_text("pass\n")
        return {}

    def review(state):
        visits.append("review")
        return {}

    graph = StateGraph(State)
    graph.add_node("quality", QualityGate())
    graph.add_node("implementation", implement)
    graph.add_node("ci-check", review)
    graph.add_edge(START, "quality")
    graph.add_conditional_edges("quality", after_quality, ["implementation", "ci-check"])
    graph.add_edge("implementation", "quality")
    graph.add_edge("ci-check", END)
    run = graph.compile().ainvoke({"workspace": str(tmp_path)})
    if repair:
        result = asyncio.run(run)
        assert visits == ["implementation", "review"]
        assert result["quality_check"]["passed"]
    else:
        with pytest.raises(QualityRetriesExhausted):
            asyncio.run(run)
        assert visits == ["implementation", "implementation"]


@pytest.mark.parametrize("kind", ["combined", "implement", "fix"])
def test_shipping_graphs_gate_every_implementation(tmp_path, kind):
    from langchain_core.runnables import RunnableLambda
    from engine.cli import starters
    from engine.runtime.workflows import load_workflow_file
    from engine.graph_runtime_langgraph.components.change_graphs import Components, implement, fix
    from langgraph_acp import ACPAgentRegistry

    configure(tmp_path, [sys.executable, "scan.py"])
    if kind == "combined":
        path = tmp_path / "graph.py"
        path.write_text(starters.source(starters.DEFAULT))
        builder = load_workflow_file(path)[0].builder
    else:
        components = Components("codex", ACPAgentRegistry(), None, None)
        builder = (implement if kind == "implement" else fix)(components).builder
    visits = []

    def stub(name):
        def invoke(state):
            visits.append(name)
            if name == "workspace":
                return {"workspace": str(tmp_path)}
            if name == "threads":
                return {"threads": [{"body": "fix"}]}
            if name == "implementation":
                # First implementation fails; the next invocation repairs it.
                (tmp_path / "scan.py").write_text("raise SystemExit(1)\n" if visits.count(name) == 1 else "pass\n")
            if name == "ci-check":
                return {"ci_check": {"passed": True}}
            if name == "reranker":
                return {"review": [], "review_rounds": 1}
            return {}
        return RunnableLambda(invoke)

    # No agent runnable is called. The real deterministic node runs on each pass.
    for name, spec in builder.nodes.items():
        if name != "quality":
            spec.runnable = stub(name)
    result = asyncio.run(builder.compile().ainvoke({"task": "fix", "inputs": {"mode": "disconnected"}}))
    assert result["quality_check"]["passed"]
    assert visits.count("implementation") == 2
    assert visits.count("ci-check") == 1
    assert visits.index("ci-check") > max(i for i, name in enumerate(visits) if name == "implementation")
    if kind != "fix":
        assert visits.index("review-security") > visits.index("ci-check")


def test_diagnostics_keep_locations_and_full_report(tmp_path):
    configure(tmp_path, [sys.executable, "-c", "print('scan.py:7:2: Q001 remove forbidden import'); raise SystemExit(1)"])
    result = run_checks(tmp_path)
    diagnostic = result["diagnostics"][0]
    assert diagnostic["file"] == "scan.py"
    assert diagnostic["line"] == 7
    assert "Q001" in diagnostic["message"]
    assert "forbidden import" in (Path(result["report"]) / "example.log").read_text()


def test_invalid_timeout_and_unknown_fields_fail(tmp_path):
    configure(tmp_path, [sys.executable, "-c", "pass"])
    config = tmp_path / ".quality.toml"
    original = config.read_text()
    for extra in ('timeout = 0\n', 'scan = "typo"\n'):
        config.write_text(original + extra)
        assert not run_checks(tmp_path)["passed"]


def test_timed_out_check_fails(tmp_path):
    configure(tmp_path, [sys.executable, "-c", "import time; time.sleep(5)"])
    with (tmp_path / ".quality.toml").open("a") as config:
        config.write("timeout = 0.01\n")
    result = run_checks(tmp_path)
    assert not result["passed"]
    assert "Unable to execute" in result["diagnostics"][0]["message"]


def test_pytest_failure_summary_is_actionable(tmp_path):
    output = 'tests/test_scan.py:42: in scan\n' + 'FAILED tests/test_scan.py::test_rule - AssertionError: forbidden import\n'
    configure(tmp_path, [sys.executable, "-c", f"print({output!r}); raise SystemExit(1)"])
    result = run_checks(tmp_path)
    assert result["diagnostics"][0]["file"] == "tests/test_scan.py"
    assert result["diagnostics"][0]["line"] == 42
    assert "test_rule" in result["diagnostics"][0]["message"]
    assert "forbidden import" in result["diagnostics"][0]["message"]

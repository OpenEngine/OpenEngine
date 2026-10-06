"""Launch the real web harness with a human-review-only workflow for live QA.

Copy this helper with --setup-file scripts/ov-smoke.py when testing older PRs.
Run with that checkout's Python after building its web client. No model, Git
fixture or external account is needed; this does not test production agents.
"""

import argparse
import importlib.util
import signal
import tempfile
from pathlib import Path


def smoke_catalog(*_):
    from engine.graph_runtime_langgraph import State, graph_workflow
    from engine.graph_runtime_langgraph.components import HumanReviewNode
    from engine.runtime import WorkflowCatalog
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(State)
    graph.add_node("human-review", HumanReviewNode())
    graph.add_edge(START, "human-review")
    graph.add_edge("human-review", END)
    return WorkflowCatalog.from_graphs((
        graph_workflow(graph, id="ov-smoke", name="OV application smoke"),
    ))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    path = project / "apps/web/e2e/harness/server.py"
    spec = importlib.util.spec_from_file_location("ov_smoke_server", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Browser harness is unavailable: {path}")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    # Select a QA workflow through the existing harness's composition seam.
    # Product sources remain untouched, including on PRs predating this helper.
    server.scripted_catalog = smoke_catalog
    with tempfile.TemporaryDirectory(prefix="ov-app-smoke-") as directory:
        root = Path(directory)
        repository = root / "repository"
        repository.mkdir()
        print("OV smoke: real app/API/storage; human-review-only QA workflow; "
              "no model execution; GitHub responses are stubbed.", flush=True)
        # Uvicorn handles shutdown, then replays SIGTERM to the prior handler.
        # Keep that replay from terminating Python before scratch cleanup runs.
        previous = signal.signal(signal.SIGTERM, lambda *_: None)
        try:
            return server.main([
                "--port", str(args.port), "--repository", str(repository),
                "--state", str(root / "state"),
            ])
        finally:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())

"""The repository's graph workflow, with a scripted agent behind it.

The browser tier's substitution, one directory over. `workflows/` is what a
deployment runs; this is what a test runs, and the difference is exactly the two
things a test must own -- which agent answers, and where its checkouts go. The
graph, the nodes, the prompts and the stages are the repository's own, called
rather than copied, so a test cannot pass against a shape production does not
have.

This is what "point the tests at a different workflow variant" means in
practice: the harness passes `--graph-workflows` naming this directory, and the
server composes whatever is in it. Nothing is switched on.

Both runner names resolve to `tests/acp_provider_fakes.py`, which reads the same
`script.json` the Codex and Claude fakes read -- so one `engine.script` call in
a spec drives either backend.
"""

import os
import sys
from pathlib import Path

from engine.graph_runtime_langgraph import agent_registry, graph_workflow
from langgraph_acp import StdioACPProvider

#: apps/web/e2e/workflows/ -> the repository's own workflow directory, which is
#: importable only by being put on the path: it is a directory of definitions
#: rather than a package, which is the whole reason a variant is a directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "workflows"))

import implementation_review_graph as production  # noqa: E402

#: Set by `apps/web/e2e/harness/server.py`: the scripted ACP agent, and the
#: directory this test's worktrees are disposable in.
AGENT = os.environ["ENGINE_E2E_ACP_AGENT"]
WORKSPACE_ROOT = os.environ["ENGINE_E2E_WORKSPACE_ROOT"]

AGENTS = agent_registry(
    StdioACPProvider(name=name, command=[AGENT]) for name in ("codex", "claude")
)

workflow = tuple(
    graph_workflow(
        production.pipeline(runner, agents=AGENTS, workspace_root=WORKSPACE_ROOT),
        id=f"implementation-review-{runner}",
        name=f"Implementation review ({runner})",
    )
    for runner in ("codex", "claude")
)

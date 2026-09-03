"""This repository's graph workflows, with their agents replaced by a fake.

The browser tier composes the real application, and the only thing it replaces
is the model. For a step workflow that is a fake CLI. For a graph workflow it
cannot be: `ACPNode` does not run `codex` or `claude`, it talks ACP to an
adapter that wraps one -- in production `npx @zed-industries/codex-acp`, which
reaches a real model over the network.

So the graphs are rebuilt through the workflow file's own `graph_for`, which
takes the two things a test has business replacing: which agents answer, and
where the checkouts are made. Same ids, same names, same stages, same prompts;
what is scripted is what the agent says back.

Here rather than in `apps/web/e2e/harness/server.py` because naming one
workflow is a thing only a test may do -- production code that special-cased
`implementation_review` is what `tests/workflow_removal_acceptance` exists to
keep gone.
"""

from __future__ import annotations

import sys
from pathlib import Path

#: tests/graph_workflow_fakes.py -> the repository root.
_ROOT = Path(__file__).resolve().parents[1]

# A workflow file is a file, not a package: the loader imports it by path. A
# test that wants the function inside one has to put the directory on the path.
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))

from engine.adapters.workspace_provider.git_worktree import (  # noqa: E402
    GitWorktreeWorkspaceProvider,
)
from engine.graph_runtime_langgraph import agent_registry  # noqa: E402
from engine.runtime import WorkflowCatalog, load_workflow_catalog  # noqa: E402
from implementation_review_graph import RUNNERS, graph_for  # noqa: E402
from langgraph_acp.providers import ClaudeACPProvider, CodexACPProvider  # noqa: E402
from provider_fakes import fake_acp  # noqa: E402


def scripted_catalog(workspace_root: str, binaries: Path) -> WorkflowCatalog:
    """Every workflow this repository ships, ready to run against fakes.

    The step workflows are loaded as they are -- their agents are CLIs, and a
    server composed for a test is already running fake ones. Only the graphs
    are rebuilt.
    """
    agent = fake_acp(binaries)
    scripted = agent_registry(
        [
            CodexACPProvider(name="codex", command=[agent]),
            ClaudeACPProvider(name="claude", command=[agent]),
        ]
    )
    loaded = load_workflow_catalog(_ROOT / "workflows")
    return WorkflowCatalog.from_definitions(
        tuple(loaded),
        tuple(
            graph_for(
                runner,
                workspace_provider=GitWorktreeWorkspaceProvider(workspace_root),
                agents=scripted,
            )
            for runner in RUNNERS
        ),
    )


__all__ = ["scripted_catalog"]

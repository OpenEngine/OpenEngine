---
title: Workflows
description: A workflow is a LangGraph graph that models your software development lifecycle. Every WorkOrder runs through one.
---

A workflow is a LangGraph graph that models your software development lifecycle. Every WorkOrder runs through one.

Each node is a stage: a checkout, an agent writing code, a reviewer, a CI check, a person signing off. Edges are the handoffs between stages, including loops back when something fails. Workflows are Python files in your repository's `workflows/` directory, so changes to your process get reviewed like any other code.

## The shipped workflow

[`implementation_review_graph.py`](https://github.com/OpenEngine/OpenEngine/blob/main/workflows/implementation_review_graph.py) is the workflow OpenEngine uses to build itself:

<ol className="flow">
  <li><span className="step">workspace</span></li><li className="arrow">→</li>
  <li><span className="step">naming</span></li><li className="arrow">→</li>
  <li><span className="step">implementation</span></li><li className="arrow">→</li>
  <li><span className="step">ci-check</span></li><li className="arrow">→</li>
  <li><span className="step">5 reviewers</span></li><li className="arrow">→</li>
  <li><span className="step">reranker</span></li><li className="arrow">→</li>
  <li><span className="step">impact-analysis</span></li><li className="arrow">→</li>
  <li><span className="step hot">human-review</span></li>
</ol>

- A CI failure goes back to implementation to fix.
- Five reviewers run in parallel: security, bugs & task adherence, performance, conciseness, and duplication.
- The reranker drops noise and duplicates, then posts what's left as pull request comments.
- Surviving findings go back to implementation for one fix-and-review round.
- Impact analysis rates the change Green, Orange or Red and posts its reasoning on the pull request.
- The run stops at human review until you approve or reject.

## Example: add a security reviewer

Say every change must be checked for server-side request forgery. Add a review facet, and the fan-out, the reranker and the model tiers pick it up automatically:

```python title="workflows/implementation_review_graph.py"
from engine.graph_runtime_langgraph.components import REVIEW_FACETS, ReviewFacet

SSRF = ReviewFacet(
    id="ssrf",
    name="SSRF",
    focus=(
        "Flag any outbound request whose URL, host, or port can be influenced "
        "by user input without an allowlist. Include redirects and webhooks."
    ),
    elevated=True,  # review with the larger model tier
)

FACETS = (*REVIEW_FACETS, SSRF)
```

Then replace `REVIEW_FACETS` with `FACETS` in the workflow file. From then on, every WorkOrder runs the SSRF reviewer alongside the other five.

## Example: a minimal workflow

Implement a task, then wait for a person. Save it in `workflows/` and it shows up in the workflow picker.

```python title="workflows/implement_and_sign_off.py"
from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_ROOT_DIRECTORY, GitWorktreeWorkspaceProvider,
)
from engine.graph_runtime_langgraph import State, TerminalMcpServer, agent_registry, graph_workflow
from engine.graph_runtime_langgraph.components import (
    ACPNode, HumanReviewNode, WorkspaceNode, checkout,
)
from langgraph.graph import END, START, StateGraph
from langgraph_acp.providers import ClaudeACPProvider

AGENTS = agent_registry([ClaudeACPProvider()])


@graph_workflow(id="implement-and-sign-off", name="Implement and sign off")
def workflow():
    builder = StateGraph(State)
    builder.add_node("workspace", WorkspaceNode(
        provider=GitWorktreeWorkspaceProvider(DEFAULT_ROOT_DIRECTORY),
        base_ref="origin/main",
    ))
    builder.add_node("implementation", ACPNode(
        agent="claude",
        registry=AGENTS,
        cwd=checkout,
        prompt=lambda state: f"Implement this and open a pull request:\n{state.get('task')}",
        mcp_server_bindings=(
            TerminalMcpServer(
                step_id="implementation",
                agent_id="claude",
                create_workorder=True,
                required_outputs=("pr_url",),
                repository_tools=("git_subcommand", "open_pull_request"),
            ),
        ),
        output_key="implementation",
        graph_node_name="Implementation",
    ))
    builder.add_node("human-review", HumanReviewNode())

    builder.add_edge(START, "workspace")
    builder.add_edge("workspace", "implementation")
    builder.add_edge("implementation", "human-review")
    builder.add_edge("human-review", END)
    return builder
```

## Building blocks

| Node | What it does |
| --- | --- |
| `WorkspaceNode` | Creates an isolated git worktree for the run. |
| `NameNode` | Gives the run a short display name. |
| `ACPNode` | Runs a coding agent (Claude Code or Codex) over ACP. |
| `CICheck` | Waits for CI and reports pass or fail. |
| `ReviewNode` | A read-only reviewer for one facet. Returns findings. |
| `RerankerNode` | Merges findings, drops noise, posts comments. |
| `HumanReviewNode` | Stops the run until a person decides. |

Anything else is plain LangGraph: conditional edges, `Send` for fan-out, your own node functions. Declare creation-time options, such as which runner implements, with `WorkflowInput`.

## Configuration

```toml title="engine.toml"
[workflows]
directory = "workflows"

[work_orders]
workflow = "implementation-review-rerank"  # default for Slack and GitHub
```

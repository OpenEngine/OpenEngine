"""Beta implementation graphs with cross-provider review fanout.

workspace -> naming -> implementation -> four reviewers -> reranker -> publish
-> human-review. Reviewers inspect independent facets without changing the tree;
only findings retained by the conservative reranker are posted to the task PR.
"""

from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.graph_runtime_langgraph import (
    GraphWorkflow,
    State,
    agent_registry,
    graph_workflow,
)
from engine.graph_runtime_langgraph.components import (
    ACPNode,
    HumanReviewNode,
    NameNode,
    ReviewNode,
    RerankerNode,
    PublishReviewNode,
    WorkspaceNode,
    checkout,
)
from engine.ports import WorkspaceProvider
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry
from langgraph_acp.providers import ClaudeACPProvider, CodexACPProvider

#: What every checkout is based on.
BASE_REF = "origin/main"

WORKSPACE = "workspace"
NAMING = "naming"
IMPLEMENTATION = "implementation"
REVIEW = "reranker"
PUBLISH = "publish"
FACETS = ("security", "bugs & task adherence", "performance", "conciseness")
REVIEW_KEYS = (
    "review-security",
    "review-bugs",
    "review-performance",
    "review-conciseness",
)
HUMAN_REVIEW = "human-review"

#: Codex and Claude, reached through their ACP adapters. `agent_registry` is
#: what routes an agent's permission request back to the run that raised it.
AGENTS = agent_registry([CodexACPProvider(), ClaudeACPProvider()])

IMPLEMENTATION_PROMPT = (
    "Implement the requested change in the provided workspace. Read the code "
    "before editing. The workspace is already based on the current remote main "
    "commit; do not fetch, pull, or merge main before editing. Make the "
    "smallest complete change and report the result.\n\n"
    "The task:\n{task}"
)


def pipeline(
    runner: str,
    *,
    workspace_provider: WorkspaceProvider | None = None,
    agents: ACPAgentRegistry = AGENTS,
) -> StateGraph:
    """Implement with the selected provider, review with the other provider."""
    if runner not in ("codex", "claude"):
        raise ValueError(f"Unsupported implementation runner: {runner}")
    reviewer = "claude" if runner == "codex" else "codex"
    regular_model = "sonnet" if reviewer == "claude" else "gpt-5.6-terra"
    security_model = "opus" if reviewer == "claude" else "gpt-5.6-sol"
    builder: StateGraph = StateGraph(State)
    builder.add_node(
        WORKSPACE,
        WorkspaceNode(
            provider=workspace_provider
            or GitWorktreeWorkspaceProvider(DEFAULT_ROOT_DIRECTORY),
            base_ref=BASE_REF,
        ),
    )
    builder.add_node(
        NAMING,
        NameNode(
            agent=runner,
            registry=agents,
            cwd=checkout,
        ),
    )
    builder.add_node(
        IMPLEMENTATION,
        ACPNode(
            agent=runner,
            registry=agents,
            prompt=lambda state: IMPLEMENTATION_PROMPT.format(
                task=state.get("task", "")
            ),
            # Work in the checkout the workspace node made. Read per run, so
            # one compiled graph serves every run.
            cwd=checkout,
            output_key=IMPLEMENTATION,
            graph_node_name="Implementation",
            graph_node_description="Makes the requested change.",
        ),
    )
    for key, facet in zip(REVIEW_KEYS, FACETS, strict=True):
        builder.add_node(
            key,
            ReviewNode(
                agent=reviewer,
                registry=agents,
                cwd=checkout,
                model=security_model if facet == "security" else regular_model,
                output_key=key,
                facet=facet,
                graph_node_name=f"Review: {facet}",
                graph_node_description=f"Inspects {facet} and produces findings.",
            ),
        )
        builder.add_edge(IMPLEMENTATION, key)
    builder.add_node(
        REVIEW,
        RerankerNode(
            agent=reviewer,
            model=regular_model,
            registry=agents,
            cwd=checkout,
            output_key=REVIEW,
            review_keys=REVIEW_KEYS,
            graph_node_name="Reranker",
            graph_node_description="Verifies findings and aggressively removes noise.",
        ),
    )
    builder.add_node(
        PUBLISH,
        PublishReviewNode(
            agent=runner,
            registry=agents,
            cwd=checkout,
            output_key=PUBLISH,
            findings_key=REVIEW,
            graph_node_name="Publish",
            graph_node_description="Publishes the PR and retained findings with lineage.",
        ),
    )
    builder.add_node(HUMAN_REVIEW, HumanReviewNode())
    builder.add_edge(START, WORKSPACE)
    builder.add_edge(WORKSPACE, NAMING)
    builder.add_edge(NAMING, IMPLEMENTATION)
    builder.add_edge(list(REVIEW_KEYS), REVIEW)
    builder.add_edge(REVIEW, PUBLISH)
    builder.add_edge(PUBLISH, HUMAN_REVIEW)
    builder.add_edge(HUMAN_REVIEW, END)
    return builder


#: One graph per agent. Picking a runner is picking one of these.
RUNNERS = ("codex", "claude")


def graph_for(
    runner: str,
    *,
    workspace_provider: WorkspaceProvider | None = None,
    agents: ACPAgentRegistry = AGENTS,
) -> GraphWorkflow:
    """This workflow, for one agent, named the way everything else names it.

    The id and the name are built in one place rather than at each call, so
    that the graph a deployment starts and the graph a test drives are the same
    graph under the same name. What a caller may replace is what `pipeline`
    accepts: where the checkouts go, and which agents answer.
    """
    return graph_workflow(
        pipeline(runner, workspace_provider=workspace_provider, agents=agents),
        id=f"implementation-review-{runner}",
        name=f"Implementation review ({runner})",
    )


workflow = tuple(graph_for(runner) for runner in RUNNERS)

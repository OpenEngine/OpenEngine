"""Implementation and review, run as a graph.

The same four stages `implementation_review.py` describes as steps, written as a
LangGraph instead:

    workspace -> implementation -> review -> human-review

Both are workflow definitions this repository owns, and which engine runs one is
decided by which kind it is -- there is no setting, and a deployment that wants
only one of them ships only one of these files. See `engine.runtime.workflows`.

What differs from the step version is what the graph runtime can do that the
workflow runtime cannot, and it is worth saying which parts are that rather than
translation:

* the checkout is a **node**, so provisioning is a position a run stands at and
  can be reported as having failed at, rather than something that happens before
  a run exists;
* the human decision is an **approval**, so the node that raised it is still
  running while a person thinks -- accepting releases it, refusing ends the run;
* an agent that stops to ask permission does so **without ending its turn**, so
  answering carries on the same conversation instead of starting a new one.

One graph per runner, because a node names the agent it runs. That is also how a
person chooses: they start a run of `implementation-review-codex` or of
`implementation-review-claude`, and the control surface lists exactly the two.
"""

from engine.adapters.workspace_provider.git_worktree import (
    GitWorktreeWorkspaceProvider,
)
from engine.graph_runtime_langgraph import (
    State,
    agent_registry,
    graph_workflow,
)
from engine.graph_runtime_langgraph.components import (
    ACPNode,
    HumanReviewNode,
    WorkspaceNode,
    checkout,
)
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry
from langgraph_acp.providers import ClaudeACPProvider, CodexACPProvider

#: Where this deployment's checkouts live. The interface's own default, stated
#: here because a workflow says where it works; a deployment that wants them
#: elsewhere changes this file.
WORKSPACE_ROOT = "/tmp/engine-workspaces"

#: What every checkout is based on.
BASE_REF = "origin/main"

WORKSPACE = "workspace"
IMPLEMENTATION = "implementation"
REVIEW = "review"
HUMAN_REVIEW = "human-review"

#: Codex and Claude, reached through their ACP adapters, with their permission
#: requests routed back to the run that raised them.
AGENTS = agent_registry([CodexACPProvider(), ClaudeACPProvider()])

IMPLEMENTATION_PROMPT = (
    "Implement the requested change in the provided workspace. Read the code "
    "before editing. The workspace is already based on the current remote main "
    "commit; do not fetch, pull, or merge main before editing. Make the "
    "smallest complete change and report the result.\n\n"
    "The task:\n{task}"
)

REVIEW_PROMPT = (
    "Review the implementation already made in the provided workspace. Read "
    "the changed code and the code around it before judging it, and check "
    "correctness, regressions the change could cause, and tests that should "
    "exist but do not. Inspect the workspace only: do not edit, revert, commit, "
    "or otherwise modify anything, and do not fix what you find. Report every "
    "finding with the file it is in and why it matters, and say so explicitly "
    "when you find nothing.\n\n"
    "Original task:\n{task}\n\n"
    "What the implementation reported:\n{implementation}"
)


def pipeline(
    runner: str,
    agents: ACPAgentRegistry = AGENTS,
    workspace_root: str = WORKSPACE_ROOT,
) -> StateGraph:
    """The four stages, with both agent nodes run by `runner`.

    The two arguments are what a *variant* of this workflow replaces and what
    nothing else should: which agents answer, and where the checkouts go. A
    test pointed at another directory reuses this rather than restating the
    graph, so it cannot pass against a shape production does not have.
    """
    builder: StateGraph = StateGraph(State)
    builder.add_node(
        WORKSPACE,
        WorkspaceNode(
            provider=GitWorktreeWorkspaceProvider(workspace_root), base_ref=BASE_REF
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
            # Every agent node works in the checkout the workspace node made, so
            # one compiled graph serves every run.
            cwd=checkout,
            output_key=IMPLEMENTATION,
            graph_node_name="Implementation",
            graph_node_description="Makes the requested change.",
        ),
    )
    builder.add_node(
        REVIEW,
        ACPNode(
            agent=runner,
            registry=agents,
            prompt=lambda state: REVIEW_PROMPT.format(
                task=state.get("task", ""),
                implementation=state.get(IMPLEMENTATION, ""),
            ),
            cwd=checkout,
            output_key=REVIEW,
            graph_node_name="Review",
            graph_node_description="Inspects the change without modifying it.",
        ),
    )
    builder.add_node(HUMAN_REVIEW, HumanReviewNode())
    builder.add_edge(START, WORKSPACE)
    builder.add_edge(WORKSPACE, IMPLEMENTATION)
    builder.add_edge(IMPLEMENTATION, REVIEW)
    builder.add_edge(REVIEW, HUMAN_REVIEW)
    builder.add_edge(HUMAN_REVIEW, END)
    return builder


workflow = tuple(
    graph_workflow(
        pipeline(runner),
        id=f"implementation-review-{runner}",
        name=f"Implementation review ({runner})",
    )
    for runner in ("codex", "claude")
)

"""Implementation and review, run as a graph.

    workspace -> naming -> implementation -> [review facets] -> reranker -> human-review

The review stage fans out to four parallel reviewers, each examining the
change from a single angle (security, bugs & task adherence, performance,
conciseness).  Their findings are collected by a *reranker* that aggressively
squashes noise and posts the survivors as PR comments with lineage.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.graph_runtime_langgraph import (
    GraphWorkflow,
    State,
    TerminalMcpServer,
    agent_registry,
    graph_workflow,
)
from engine.graph_runtime_langgraph.components import (
    ACPNode,
    HumanReviewNode,
    NameNode,
    WorkspaceNode,
    checkout,
)
from engine.ports import WorkspaceProvider
from langgraph.types import Send
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry
from langgraph_acp.providers import ClaudeACPProvider, CodexACPProvider


# ---------------------------------------------------------------------------
# Finding: first-class review output
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Finding:
    """One reviewer observation, structured for comment posting and reranking.

    Each review facet produces findings; the reranker filters them and posts
    the survivors as PR comments with lineage.
    """

    tagline: str
    """1-2 lines, as explained to a layman."""
    description: str
    """1-3 followup lines about what it is and why it's bad."""
    facet: str = ""
    """Which review facet produced this: security, bugs, performance, conciseness."""
    agent: str = ""
    """Which agent produced this: codex, claude."""
    file: str | None = None
    """The file this finding relates to, if any."""
    line: int | None = None
    """The line number, if applicable."""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"tagline": self.tagline, "description": self.description}
        if self.facet:
            d["facet"] = self.facet
        if self.agent:
            d["agent"] = self.agent
        if self.file is not None:
            d["file"] = self.file
        if self.line is not None:
            d["line"] = self.line
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            tagline=str(data.get("tagline", "")),
            description=str(data.get("description", "")),
            facet=str(data.get("facet", "")),
            agent=str(data.get("agent", "")),
            file=data.get("file"),
            line=data.get("line"),
        )

    def as_comment(self) -> str:
        """Format for posting as a PR comment, with lineage."""
        parts = [f"**{self.tagline}**", "", self.description]
        lineage: list[str] = []
        if self.agent:
            lineage.append(f"agent: {self.agent}")
        if self.facet:
            lineage.append(f"facet: {self.facet}")
        if lineage:
            parts.extend(["", f"_Produced by {', '.join(lineage)}_"])
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Review facets
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ReviewFacet:
    """One angle a reviewer examines code from."""

    id: str
    """Short identifier used as node name suffix and state key."""
    name: str
    """Human-readable name for prompts and lineage."""
    focus: str
    """What the reviewer should look for, phrased as instructions."""
    elevated: bool = False
    """Whether this facet uses the elevated (larger) model tier."""


#: The four review facets.  Security uses a larger model; the rest use the
#: default tier.
REVIEW_FACETS: tuple[ReviewFacet, ...] = (
    ReviewFacet(
        id="security",
        name="Security",
        focus=(
            "Look for security vulnerabilities: injection flaws (SQL, command, "
            "XSS), authentication and authorization gaps, secrets or credentials "
            "in code, unsafe deserialization, path traversal, and any OWASP Top 10 "
            "issues. Evaluate whether inputs from external sources are validated "
            "and sanitized."
        ),
        elevated=True,
    ),
    ReviewFacet(
        id="bugs",
        name="Bugs & task adherence",
        focus=(
            "Look for correctness bugs, logic errors, off-by-one mistakes, null/"
            "None handling gaps, race conditions, and missing error handling. Also "
            "verify that the implementation actually does what the original task "
            "asked for -- flag anything the task requested that is missing, and "
            "anything present that the task did not ask for."
        ),
    ),
    ReviewFacet(
        id="performance",
        name="Performance",
        focus=(
            "Look for performance issues: unnecessary allocations, O(n^2) or worse "
            "algorithms where better ones are straightforward, missing indexes on "
            "queries, unbounded loops, blocking calls in async contexts, and "
            "resource leaks (open files, connections, memory)."
        ),
    ),
    ReviewFacet(
        id="conciseness",
        name="Conciseness",
        focus=(
            "Look for unnecessary complexity: dead code, redundant conditions, "
            "over-abstraction, copy-paste duplication that should be a shared "
            "function, overly verbose patterns that have a simpler equivalent in "
            "the language or framework, and any code that could be removed without "
            "changing behavior."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Graph constants
# ---------------------------------------------------------------------------

#: What every checkout is based on.
BASE_REF = "origin/main"

WORKSPACE = "workspace"
NAMING = "naming"
IMPLEMENTATION = "implementation"
REVIEW = "review"
RERANKER = "reranker"
HUMAN_REVIEW = "human-review"

#: Codex and Claude, reached through their ACP adapters.  `agent_registry` is
#: what routes an agent's permission request back to the run that raised it.
AGENTS = agent_registry([CodexACPProvider(), ClaudeACPProvider()])

#: Model identifiers per runner.  The default tier handles most facets; the
#: elevated tier handles security, where missing something costs more.
#:
#: Claude reviewers are sonnet-sized by default, opus-sized for security.
#: Codex reviewers are terra-sized by default, sol-sized for security.
REVIEW_MODELS: dict[str, dict[str, str]] = {
    "claude": {"default": "claude-sonnet-4-6", "elevated": "claude-opus-4-6"},
    "codex": {"default": "codex-mini-latest", "elevated": "o3"},
}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

IMPLEMENTATION_PROMPT = (
    "Implement the requested change in the provided workspace. Read the code "
    "before editing. The workspace is already based on the current remote main "
    "commit; do not fetch, pull, or merge main before editing. Make the "
    "smallest complete change and report the result.\n\n"
    "Every git operation goes through the git_subcommand tool. When the change "
    "is ready, create a descriptive agent/<description> branch, commit only this "
    "change, push that branch, then call open_pull_request. Finish by calling "
    "complete_step with the pull request URL as the pr_url output. Use fail_step "
    "if the work cannot be completed, or clarify only when answering a question "
    "without changing the implementation.\n\n"
    "The task:\n{task}"
)

FACET_REVIEW_PROMPT = (
    "Review the implementation in this workspace, focusing exclusively on "
    "**{facet_name}**.\n\n"
    "{facet_focus}\n\n"
    "Read the changed code and the code around it before judging. Inspect "
    "only: do not edit, revert, commit, or modify anything.\n\n"
    "Your acceptance criteria: produce a JSON array of *finding* objects. "
    "Each finding has exactly two required fields:\n"
    '- "tagline": 1-2 lines explaining the issue as you would to a layman\n'
    '- "description": 1-3 followup lines about what it is and why it is bad\n\n'
    "Optional fields (include when applicable):\n"
    '- "file": the file path the finding relates to\n'
    '- "line": the line number within that file\n\n'
    "Call complete_step with the findings output set to the JSON array. "
    "If you find nothing worth reporting, pass an empty array [].\n\n"
    "Original task:\n{task}\n\n"
    "What the implementation reported:\n{implementation}"
)

RERANKER_PROMPT = (
    "You are a senior reviewer consolidating findings from {reviewer_count} "
    "specialized reviewers who each examined the same code change from a "
    "different angle. Your job is to **aggressively** squash noise.\n\n"
    "Remove any finding that is:\n"
    "- A nitpick or stylistic preference\n"
    "- A duplicate of another finding (even across facets)\n"
    "- About a hypothetical issue that is extremely unlikely in practice\n"
    "- Not actionable -- the author cannot do anything concrete about it\n"
    "- Already handled by existing code the reviewer missed\n\n"
    "Keep only findings a senior engineer would genuinely want fixed before "
    "merging. When in doubt, remove the finding.\n\n"
    "For each surviving finding, post it as a PR comment using add_comment. "
    "Format each comment as:\n\n"
    "**<tagline>**\n\n"
    "<description>\n\n"
    "_Produced by {runner} reviewing <facet>_\n\n"
    "Use the file and line from the finding for inline comments where "
    "available; use a general comment otherwise. If no findings survive, "
    "leave one general comment saying the change looks clean.\n\n"
    "After posting comments, call complete_step with the filtered findings "
    "as a JSON array (same schema as the inputs).\n\n"
    "{findings_sections}"
    "Original task:\n{task}"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _with_model(
    base: Mapping[str, object] | None, model: str
) -> dict[str, object]:
    """Merge a model override into the caller's session config."""
    merged = dict(base or {})
    merged["model"] = model
    return merged


def _review_node_name(facet_id: str) -> str:
    return f"review-{facet_id}"


def _fan_out_reviews(state: dict[str, Any]) -> list[Send]:
    """Dispatch the implementation to all four review facets in parallel."""
    return [Send(_review_node_name(facet.id), state) for facet in REVIEW_FACETS]


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def pipeline(
    runner: str,
    *,
    workspace_provider: WorkspaceProvider | None = None,
    agents: ACPAgentRegistry = AGENTS,
    session_config: Mapping[str, object] | None = None,
) -> StateGraph:
    """The stages, with every agent node run by `runner`.

    The three keyword arguments are the only things a deployment or a test has
    business replacing: where the checkouts are made, which agents answer, and
    what session settings (attribution, output style) the adapter should apply.
    """
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
            session_config=session_config,
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
            cwd=checkout,
            mcp_server_bindings=(
                TerminalMcpServer(
                    step_id=IMPLEMENTATION,
                    agent_id=runner,
                    required_outputs=("pr_url",),
                ),
            ),
            output_key=IMPLEMENTATION,
            graph_node_name="Implementation",
            graph_node_always_open=True,
            graph_node_description="Makes the requested change.",
            session_config=session_config,
        ),
    )

    # ---- review fan-out: one node per facet, run in parallel ----------------

    models = REVIEW_MODELS.get(runner, REVIEW_MODELS["claude"])
    for facet in REVIEW_FACETS:
        model = models["elevated"] if facet.elevated else models["default"]
        node_name = _review_node_name(facet.id)
        builder.add_node(
            node_name,
            ACPNode(
                agent=runner,
                registry=agents,
                prompt=lambda state, f=facet: FACET_REVIEW_PROMPT.format(
                    facet_name=f.name,
                    facet_focus=f.focus,
                    task=state.get("task", ""),
                    implementation=state.get(IMPLEMENTATION, ""),
                ),
                cwd=checkout,
                mcp_server_bindings=(
                    TerminalMcpServer(
                        step_id=node_name,
                        agent_id=runner,
                        required_outputs=("findings",),
                        repository_tools=(
                            "view_change_request",
                            "list_pipeline_status",
                            "get_job_logs",
                        ),
                    ),
                ),
                output_key=node_name,
                graph_node_name=f"Review ({facet.name})",
                graph_node_description=(
                    f"Reviews the change for {facet.name.lower()}."
                ),
                session_config=_with_model(session_config, model),
            ),
        )

    # ---- reranker: squash noise and post comments ---------------------------

    def _reranker_prompt(state: Mapping[str, object]) -> str:
        sections: list[str] = []
        for facet in REVIEW_FACETS:
            key = _review_node_name(facet.id)
            sections.append(
                f"Findings from {facet.name} review:\n"
                f"{state.get(key, '(none)')}\n\n"
            )
        return RERANKER_PROMPT.format(
            reviewer_count=len(REVIEW_FACETS),
            runner=runner,
            findings_sections="".join(sections),
            task=state.get("task", ""),
        )

    builder.add_node(
        RERANKER,
        ACPNode(
            agent=runner,
            registry=agents,
            prompt=_reranker_prompt,
            cwd=checkout,
            mcp_server_bindings=(
                TerminalMcpServer(
                    step_id=RERANKER,
                    agent_id=runner,
                    required_outputs=("findings",),
                    repository_tools=(
                        "view_change_request",
                        "list_pipeline_status",
                        "get_job_logs",
                        "add_comment",
                    ),
                ),
            ),
            output_key=REVIEW,
            graph_node_name="Reranker",
            graph_node_description=(
                "Consolidates review findings and posts the survivors."
            ),
            session_config=session_config,
        ),
    )

    builder.add_node(HUMAN_REVIEW, HumanReviewNode())

    # ---- edges --------------------------------------------------------------

    builder.add_edge(START, WORKSPACE)
    builder.add_edge(WORKSPACE, NAMING)
    builder.add_edge(NAMING, IMPLEMENTATION)

    # Fan-out: implementation dispatches to all review facets in parallel.
    builder.add_conditional_edges(IMPLEMENTATION, _fan_out_reviews)

    # Fan-in: every facet converges on the reranker.
    for facet in REVIEW_FACETS:
        builder.add_edge(_review_node_name(facet.id), RERANKER)

    builder.add_edge(RERANKER, HUMAN_REVIEW)
    builder.add_edge(HUMAN_REVIEW, END)
    return builder


#: One graph per agent. Picking a runner is picking one of these.
RUNNERS = ("codex", "claude")


def graph_for(
    runner: str,
    *,
    workspace_provider: WorkspaceProvider | None = None,
    agents: ACPAgentRegistry = AGENTS,
    session_config: Mapping[str, object] | None = None,
) -> GraphWorkflow:
    """This workflow, for one agent, named the way everything else names it."""
    return graph_workflow(
        pipeline(
            runner,
            workspace_provider=workspace_provider,
            agents=agents,
            session_config=session_config,
        ),
        id=f"implementation-review-{runner}",
        name=f"Implementation review ({runner})",
    )


workflow = tuple(graph_for(runner) for runner in RUNNERS)

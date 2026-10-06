"""Implementation and review, run as a graph.

    workspace -> naming -> implementation -> ci-check -> [review facets] -> reranker -> impact-analysis -> human-review

The review stage fans out to five parallel reviewers, each examining the
change from a single angle (security, bugs & task adherence, performance,
conciseness, DRYness & code duplication). Their findings are collected by a
*reranker* that somewhat aggressively squashes noise and posts the survivors
as PR comments with lineage.
Surviving findings go back to implementation for one automatic fix-and-review
cycle before impact analysis and human review.

A run can instead start in the review state (`engine.domain.states`), pointed
at an existing change -- a pull request, or a branch -- through the `ref` and
`pr_url` inputs. It is checked out at that change and goes straight to review:

    workspace -> [review facets] -> reranker -> triage -> (implementation -> ci-check -> review ...)

Nothing is posted; a person is shown the surviving findings at *triage* and
chooses which to fix, and each fix is reviewed again before triage asks again.

A review requested from Engine on the forge sets the `publish_review` input
instead: whoever asked reads the review on the pull request, so the survivors
are posted there and impact analysis posts its rating, and nothing waits:

    workspace -> [review facets] -> reranker -> impact-analysis

The workflow offers both forge modes (`engine.domain.forge`) through
`mode_input`. Nothing here branches on the mode: the components narrow the
tools and skip CI themselves, and the prompts are filled from the shared
`components.forge` snippets that say where the work goes.
"""

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_BRANCH_REF,
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.graph_runtime_langgraph import (
    GraphWorkflow,
    WorkflowInput,
    State,
    TerminalMcpServer,
    agent_registry,
    graph_workflow,
)
from engine.graph_runtime_langgraph.components import (
    ACPNode,
    CICheck,
    REVIEW_FACETS,
    HumanReviewNode,
    NameNode,
    OpenVerify,
    RerankerNode,
    ReviewNode,
    TriageNode,
    WorkspaceNode,
    checkout,
)
from engine.graph_runtime_langgraph.components.forge import (
    ANSWER_REVIEW,
    CHANGE_UNDER_REVIEW,
    PUBLISH_CHANGE,
    PUBLISH_FINDINGS,
    PUBLISH_SUMMARY,
    THE_CHANGE,
    UPDATE_CHANGE,
)
from engine.domain import (
    REVIEW_BRANCH_INPUT, REVIEW_PR_INPUT, REVIEW_PUBLISH_INPUT, REVIEW_REF_INPUT,
    ForgeMode, StepCompleted, WorkState, forge_mode, publishes_review, start_state,
)
from engine.graph_runtime.inputs import (
    LEAST_UTILIZED, ROUND_ROBIN, mode_input, state_input,
)
from engine.graph_runtime_langgraph.executions import NodeExecution
from engine.ports import ApprovalHandler, WorkspaceProvider
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from langgraph_acp import ACPAgentRegistry
from langgraph_acp.providers import ClaudeACPProvider, CodexACPProvider


# ---------------------------------------------------------------------------
# Graph constants
# ---------------------------------------------------------------------------

#: What every checkout is based on: the default branch of the run's repository,
#: since one workflow serves every repository under `[repos]`.
BASE_REF = DEFAULT_BRANCH_REF

WORKSPACE = "workspace"
NAMING = "naming"
IMPLEMENTATION = "implementation"
CI_CHECK = "ci-check"
REVIEW = "review"
RERANKER = "reranker"
IMPACT_ANALYSIS = "impact-analysis"
HUMAN_REVIEW = "human-review"
VERIFICATION = "verification"
TRIAGE = "triage"

#: Where the findings a person chose at triage are kept for implementation.
FIX = "fix"

#: The creation inputs naming the change a run started in review looks at.
REF_INPUT, PR_INPUT, BRANCH_INPUT = REVIEW_REF_INPUT, REVIEW_PR_INPUT, REVIEW_BRANCH_INPUT
#: Set on a run started in review to post its review rather than triage it.
PUBLISH_INPUT = REVIEW_PUBLISH_INPUT

#: Codex and Claude, reached through their ACP adapters.  `agent_registry` is
#: what routes an agent's permission request back to the run that raised it.
AGENTS = agent_registry([CodexACPProvider(), ClaudeACPProvider()])

#: Model identifiers per runner.  The default tier handles most facets; the
#: elevated tier handles security, where missing something costs more.
#:
#: Claude reviewers are sonnet-sized by default, opus-sized for security.
#: Tier aliases intentionally track the current Claude model of each size.
#: Codex reviewers are terra-sized by default, sol-sized for security.
REVIEW_MODELS: dict[str, dict[str, str]] = {
    "claude": {"default": "sonnet", "elevated": "opus"},
    "codex": {"default": "gpt-5.6-terra", "elevated": "gpt-5.6-sol"},
}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

IMPLEMENTATION_PROMPT = (
    "Implement the requested change in the provided workspace. Read the code "
    "before editing. The workspace is already based on the current commit of the "
    "remote default branch; do not fetch, pull, or merge it before editing. Make the "
    "smallest complete change and report the result.\n\n"
    "{publish}"
    "Use fail_step "
    "if the work cannot be completed, or clarify only when answering a question "
    "without changing the implementation.\n\n"
    "The task:\n{task}"
)

FACET_REVIEW_PROMPT = (
    "Review the implementation in this workspace, focusing exclusively on "
    "**{facet_name}**.\n\n"
    "{facet_focus}\n\n"
    "The change is {change}. "
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
    "different angle. Your job is to **somewhat aggressively** squash noise.\n\n"
    "Remove any finding that is:\n"
    "- A nitpick or stylistic preference\n"
    "- A duplicate of another finding (even across facets)\n"
    "- About a hypothetical issue that is extremely unlikely in practice\n"
    "- Not actionable -- the author cannot do anything concrete about it\n"
    "- Already handled by existing code the reviewer missed\n\n"
    "Keep findings a senior engineer would genuinely want fixed before "
    "merging, and concrete, actionable findings that are likely real even when "
    "they are minor. When in doubt about one of those, keep it.\n\n"
    "{publishing}"
    "{findings_sections}"
    "{change}"
    "Original task:\n{task}"
)

IMPACT_ANALYSIS_PROMPT = (
    "Assess the impact of the final change {change}. Read the "
    "diff and surrounding code, tests, CI results, and review findings. Inspect "
    "only: do not edit, commit, merge, or deploy anything.\n\n"
    "Rank the change at exactly one level:\n"
    "Green 🟢: No significant UI or architectural changes. Administrative "
    "changes, narrowly scoped bug fixes, or minor product behavior changes. "
    "Well tested with few testing blind spots, and no new integrated component "
    "requiring human setup.\n"
    "Orange 🟠: Moderate UI adjustments, a new architectural component, or "
    "significant code changes to prevent a bug. Well tested, but a new "
    "integrated component may require human setup, or integration testing "
    "may be incomplete for a known reason.\n"
    "Red 🔴: Significant architectural alterations, a major new feature, or "
    "excess complexity. May touch sensitive components such as security or "
    "auth, and requires human involvement to deploy. Requires careful review "
    "and must not be merged without a human.\n\n"
    "Choose the highest applicable level; passing tests alone does not lower "
    "the impact. Explain uncertainty and testing gaps rather than assuming "
    "untested behavior is safe. Give evidence for UI/product and architectural "
    "scope, complexity, sensitive components, test coverage and blind spots, "
    "and human setup or deployment work.\n\n"
    "{publishing}"
    "call complete_step with impact_level set to exactly "
    "Green, Orange, or Red "
    "and impact_rationale containing your evidence and required human actions. "
    "Include the color label and emoji and the rationale in the summary.\n\n"
    "Original task:\n{task}\n\nImplementation report:\n{implementation}\n\n"
    "CI results:\n{ci}\n\nFinal review findings:\n{findings}"
)

#: How a run started in review publishes its consolidated findings: it does not.
KEEP_FINDINGS = (
    "Do not post comments or contact any pull request: the surviving findings "
    "are shown to a person, who chooses which to fix or post. Call "
    "complete_step with the filtered findings as a JSON array (same schema as "
    "the inputs), or [] when none survive. Preserve each finding's agent and "
    "facet fields unchanged.\n\n"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RunnerInput:
    """Resolve the stage's runner per invocation, including its MCP identity."""

    graph_node_runner_input = "implementation_runner"

    def _for_runner(self, runner: str) -> ACPNode:
        config = self.session_config
        if isinstance(self, ReviewNode):
            facet = next(f for f in REVIEW_FACETS if f.id == self.facet)
            model = REVIEW_MODELS[runner]["elevated" if facet.elevated else "default"]
            config = _with_model(config, model)
        return replace(
            self,
            agent=runner,
            session_config=config,
            mcp_server_bindings=tuple(
                replace(binding, agent_id=runner)
                if isinstance(binding, TerminalMcpServer) else binding
                for binding in self.mcp_server_bindings
            ),
        )


class InputImplementationNode(_RunnerInput, ACPNode):
    pass


class InputNameNode(_RunnerInput, NameNode):
    pass


class InputImpactAnalysisNode(_RunnerInput, ACPNode):
    """Keep a validated impact rating and its evidence in checkpoint state."""

    graph_node_runner_input = "review_runner"


def _validate_impact_analysis(event: StepCompleted) -> None:
    outputs = {output.name: output.value for output in event.outputs}
    if outputs.get("impact_level") not in ("Green", "Orange", "Red"):
        raise ValueError("impact_level must be Green, Orange, or Red")
    rationale = outputs.get("impact_rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError("impact_rationale must be non-empty")


class InputReviewNode(_RunnerInput, ReviewNode):
    graph_node_runner_input = "review_runner"


class _RerankerTools(TerminalMcpServer):
    """The reranker's tools, without `add_comment` in a run that keeps its findings.

    `KEEP_FINDINGS` asks it not to post, so the tool is withheld by the server
    rather than only by the prompt.
    """

    def __call__(
        self,
        state: Mapping[str, object],
        execution: NodeExecution,
        approve: ApprovalHandler,
    ) -> Any:
        return TerminalMcpServer.__call__(self.for_state(state), state, execution, approve)

    def for_state(self, state: Mapping[str, object]) -> TerminalMcpServer:
        if not _keeping_findings(state):
            return self
        return replace(self, repository_tools=tuple(
            name for name in self.repository_tools if name != "add_comment"
        ))


class InputRerankerNode(_RunnerInput, RerankerNode):
    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        update = await super().__call__(state)
        return {**update, "review_rounds": state.get("review_rounds", 0) + 1}


def _with_model(
    base: Mapping[str, object] | None, model: str
) -> dict[str, object]:
    """Merge a model override into the caller's session config."""
    merged = dict(base or {})
    merged["model"] = model
    return merged


def _review_node_name(facet_id: str) -> str:
    return f"review-{facet_id}"


def _reviewing(state: Mapping[str, object]) -> bool:
    """Whether this run started in review, at a change it did not make."""
    return start_state(state.get("inputs")) is WorkState.REVIEW


def _publishing(state: Mapping[str, object]) -> bool:
    """Whether a run started in review posts its review instead of triaging it."""
    return _reviewing(state) and publishes_review(state.get("inputs"))


def _keeping_findings(state: Mapping[str, object]) -> bool:
    """Whether the surviving findings are left for a person at triage."""
    return _reviewing(state) and not _publishing(state)


def _pr_url(state: Mapping[str, object]) -> str:
    """The run's pull request: the one it opened, or the one it was given."""
    inputs = state.get("inputs")
    given = inputs.get(PR_INPUT, "") if isinstance(inputs, Mapping) else ""
    return str(state.get("pr_url") or given or "")


def _push_to(state: Mapping[str, object]) -> str:
    """Where a fix to a change this run was given goes: that change's branch.

    The workspace is checked out on a branch of its own, so a plain push would
    land beside the pull request rather than on it.
    """
    inputs = state.get("inputs")
    branch = str(inputs.get(BRANCH_INPUT) or "") if isinstance(inputs, Mapping) else ""
    if not (_reviewing(state) and branch and forge_mode(inputs) is ForgeMode.CONNECTED):
        return ""
    return f"The pull request's branch is {branch}: push to it with `git push origin HEAD:{branch}`. "


def _implementation_prompt(state: Mapping[str, object]) -> str:
    ci = state.get("ci_check")
    if isinstance(ci, dict) and ci.get("passed") is False:
        return (
            f"Fix the CI failures on {THE_CHANGE(state, pr_url=_pr_url(state))}. "
            "Read the failed job logs and relevant code, make the smallest fix, "
            f"test it, {UPDATE_CHANGE(state)}{_push_to(state)}"
            "Use fail_step if the failures cannot be fixed.\n\n"
            f"{ci.get('summary', '')}\n\nOriginal task:\n{state.get('task', '')}"
        )
    findings = state.get(FIX) if _reviewing(state) else (
        state.get(REVIEW) if state.get("review_rounds") == 1 else None
    )
    if findings:
        return (
            "Address the review findings on "
            f"{THE_CHANGE(state, pr_url=_pr_url(state))}. "
            "Read the relevant code, make the smallest complete fix, test it, "
            f"{UPDATE_CHANGE(state)}{_push_to(state)}"
            # Findings a person chose were never posted, so there is nothing to answer.
            f"{'' if _reviewing(state) else ANSWER_REVIEW(state)}"
            "Use fail_step if the findings cannot be addressed.\n\n"
            f"Review findings:\n{json.dumps(findings)}\n\n"
            f"Original task:\n{state.get('task', '')}"
        )
    return IMPLEMENTATION_PROMPT.format(
        publish=PUBLISH_CHANGE(state), task=state.get("task", ""),
    )


def _after_workspace(state: dict[str, Any]) -> str | list[Send]:
    if _reviewing(state):
        return _fan_out_reviews(state)
    return NAMING


def _after_reranker(state: dict[str, Any]) -> str:
    if _reviewing(state):
        return IMPACT_ANALYSIS if _publishing(state) else TRIAGE
    if state.get(REVIEW) and state.get("review_rounds") == 1:
        return IMPLEMENTATION
    return IMPACT_ANALYSIS


def _after_impact_analysis(state: dict[str, Any]) -> str:
    # A change this run did not make has no work order for a person to accept.
    return END if _publishing(state) else HUMAN_REVIEW


def _after_triage(state: dict[str, Any]) -> str:
    return IMPLEMENTATION if state.get(FIX) else END


def _after_ci(state: dict[str, Any]) -> str | list[Send]:
    if not state["ci_check"]["passed"]:
        return IMPLEMENTATION
    return _fan_out_reviews(state)


def _fan_out_reviews(state: dict[str, Any]) -> list[Send]:
    """Dispatch the implementation to all review facets in parallel."""
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
    verification: OpenVerify | None = None,
) -> StateGraph:
    """Use declared stage inputs, defaulting to `runner` and the other provider.

    Deployments can replace checkout provisioning, agents and session settings,
    and opt into Open Verify by supplying its installed CLI and evidence uploader.
    """
    builder: StateGraph = StateGraph(State)
    builder.add_node(
        WORKSPACE,
        WorkspaceNode(
            provider=workspace_provider
            or GitWorktreeWorkspaceProvider(DEFAULT_ROOT_DIRECTORY),
            base_ref=BASE_REF,
            ref_input=REF_INPUT,
        ),
    )
    builder.add_node(
        NAMING,
        InputNameNode(
            agent=runner,
            registry=agents,
            cwd=checkout,
            session_config=session_config,
        ),
    )
    builder.add_node(
        IMPLEMENTATION,
        InputImplementationNode(
            agent=runner,
            registry=agents,
            prompt=_implementation_prompt,
            cwd=checkout,
            mcp_server_bindings=(
                TerminalMcpServer(
                    step_id=IMPLEMENTATION,
                    create_workorder=True,
                    agent_id=runner,
                    required_outputs=("pr_url",),
                    repository_tools=(
                        "git_subcommand", "open_pull_request",
                        "list_pipeline_status", "get_job_logs",
                    ),
                ),
            ),
            output_key=IMPLEMENTATION,
            graph_node_name="Implementation",
            graph_node_always_open=True,
            graph_node_description="Makes the requested change.",
            graph_node_group=WorkState.IMPLEMENTATION,
            session_config=session_config,
        ),
    )

    builder.add_node(CI_CHECK, CICheck())

    # ---- review fan-out: one node per facet, run in parallel ----------------

    reviewer = {"codex": "claude", "claude": "codex"}[runner]
    models = REVIEW_MODELS[reviewer]
    for facet in REVIEW_FACETS:
        model = models["elevated"] if facet.elevated else models["default"]
        node_name = _review_node_name(facet.id)
        builder.add_node(
            node_name,
            InputReviewNode(
                facet=facet.id,
                agent=reviewer,
                registry=agents,
                prompt=lambda state, f=facet: FACET_REVIEW_PROMPT.format(
                    facet_name=f.name,
                    facet_focus=f.focus,
                    change=CHANGE_UNDER_REVIEW(state, pr_url=_pr_url(state)),
                    task=state.get("task", ""),
                    implementation=state.get(IMPLEMENTATION, ""),
                ),
                cwd=checkout,
                mcp_server_bindings=(
                    TerminalMcpServer(
                        step_id=node_name,
                        agent_id=reviewer,
                        required_outputs=("findings",),
                        validate_completion=ReviewNode.validate_completion,
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
                f"{json.dumps(state.get(key, []))}\n\n"
            )
        return RERANKER_PROMPT.format(
            reviewer_count=len(REVIEW_FACETS),
            publishing=KEEP_FINDINGS if _keeping_findings(state) else PUBLISH_FINDINGS(
                state, runner=state.get("inputs", {}).get("review_runner", reviewer),
            ),
            findings_sections="".join(sections),
            change=f"The change is {CHANGE_UNDER_REVIEW(state, pr_url=_pr_url(state))}.\n\n",
            task=state.get("task", ""),
        )

    builder.add_node(
        RERANKER,
        InputRerankerNode(
            agent=runner,
            registry=agents,
            prompt=_reranker_prompt,
            cwd=checkout,
            mcp_server_bindings=(
                _RerankerTools(
                    step_id=RERANKER,
                    create_workorder=True,
                    agent_id=runner,
                    required_outputs=("findings",),
                    validate_completion=RerankerNode.validate_completion,
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

    builder.add_node(
        IMPACT_ANALYSIS,
        InputImpactAnalysisNode(
            agent=reviewer,
            registry=agents,
            prompt=lambda state: IMPACT_ANALYSIS_PROMPT.format(
                change=CHANGE_UNDER_REVIEW(state, pr_url=_pr_url(state)),
                publishing=PUBLISH_SUMMARY(state, pr_url=_pr_url(state)),
                task=state.get("task", ""),
                implementation=state.get(IMPLEMENTATION, ""),
                ci=json.dumps(state.get("ci_check", {})),
                findings=json.dumps(state.get(REVIEW, [])),
            ),
            cwd=checkout,
            mcp_server_bindings=(
                TerminalMcpServer(
                    step_id=IMPACT_ANALYSIS,
                    agent_id=reviewer,
                    required_outputs=("impact_level", "impact_rationale"),
                    validate_completion=_validate_impact_analysis,
                    repository_tools=(
                        "view_change_request", "list_pipeline_status", "get_job_logs",
                        "add_comment",
                    ),
                ),
            ),
            output_key=IMPACT_ANALYSIS,
            graph_node_name="Impact analysis",
            graph_node_description="Ranks change impact as Green 🟢, Orange 🟠, or Red 🔴.",
            graph_node_group=WorkState.REVIEW,
            session_config=session_config,
        ),
    )
    builder.add_node(HUMAN_REVIEW, HumanReviewNode())
    builder.add_node(TRIAGE, TriageNode(findings_key=REVIEW, output_key=FIX))

    # ---- edges --------------------------------------------------------------

    builder.add_edge(START, WORKSPACE)
    builder.add_conditional_edges(
        WORKSPACE, _after_workspace,
        [NAMING, *(_review_node_name(f.id) for f in REVIEW_FACETS)],
    )
    builder.add_edge(NAMING, IMPLEMENTATION)

    builder.add_edge(IMPLEMENTATION, CI_CHECK)
    builder.add_conditional_edges(
        CI_CHECK, _after_ci,
        [IMPLEMENTATION, *(_review_node_name(f.id) for f in REVIEW_FACETS)],
    )

    # Fan-in: every facet converges on the reranker.
    for facet in REVIEW_FACETS:
        builder.add_edge(_review_node_name(facet.id), RERANKER)

    builder.add_conditional_edges(
        RERANKER, _after_reranker, [IMPLEMENTATION, IMPACT_ANALYSIS, TRIAGE],
    )
    builder.add_conditional_edges(TRIAGE, _after_triage, [IMPLEMENTATION, END])
    if verification is not None:
        builder.add_node(VERIFICATION, verification)
        builder.add_conditional_edges(
            IMPACT_ANALYSIS, _after_impact_analysis,
            {HUMAN_REVIEW: VERIFICATION, END: END},
        )
        builder.add_edge(VERIFICATION, HUMAN_REVIEW)
    else:
        builder.add_conditional_edges(
            IMPACT_ANALYSIS, _after_impact_analysis, [HUMAN_REVIEW, END],
        )
    builder.add_edge(HUMAN_REVIEW, END)
    return builder


#: Runner choices for the implementation and review stages.
RUNNER_CHOICES = ("codex", "claude")
#: What the runner dropdowns offer: a runner, or a policy that picks one per run.
RUNNER_INPUT_CHOICES = (*RUNNER_CHOICES, LEAST_UTILIZED, ROUND_ROBIN)
#: What this workflow was called when the runner was part of its id, before the
#: stages became creation inputs. WorkOrders started then remember one of these,
#: so they are retired rather than dropped and go on opening as this workflow.
PREVIOUS_IDS = ("implementation-review-codex", "implementation-review-claude")
# The loader rebuilds one default graph with deployment session configuration.
RUNNERS = ("codex",)


def graph_for(
    runner: str,
    *,
    workspace_provider: WorkspaceProvider | None = None,
    agents: ACPAgentRegistry = AGENTS,
    session_config: Mapping[str, object] | None = None,
    verification: OpenVerify | None = None,
) -> GraphWorkflow:
    """Build the workflow with the requested initial implementation runner."""
    return graph_workflow(
        pipeline(
            runner,
            workspace_provider=workspace_provider,
            agents=agents,
            session_config=session_config,
            verification=verification,
        ),
        id="implementation-review-rerank",
        name="Implementation review rerank",
        previous_ids=PREVIOUS_IDS,
        inputs=(
            WorkflowInput(
                "implementation_runner", "Implementation runner",
                default=runner, required=True, choices=RUNNER_INPUT_CHOICES,
            ),
            WorkflowInput(
                "review_runner", "Review runner",
                default={"codex": "claude", "claude": "codex"}[runner],
                required=True, choices=RUNNER_INPUT_CHOICES,
            ),
            mode_input(),
            state_input(WorkState.PLANNING, WorkState.REVIEW),
            WorkflowInput(REF_INPUT, "Ref to review"),
            WorkflowInput(PR_INPUT, "Pull request to review"),
            WorkflowInput(BRANCH_INPUT, "Pull request branch"),
            WorkflowInput(PUBLISH_INPUT, "Post the review to the pull request"),
        ),
    )


workflow = graph_for("codex")

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

The workflow offers both forge modes (`engine.domain.forge`) through
`mode_input`. Nothing here branches on the mode: the components narrow the
tools and skip CI themselves, and the prompts are filled from the shared
`components.forge` snippets that say where the work goes.
"""

import json

from engine.graph_runtime_langgraph.components.change_prompts import (
    IMPLEMENTATION_PROMPT, FACET_REVIEW_PROMPT, RERANKER_PROMPT,
    IMPACT_ANALYSIS_PROMPT, KEEP_FINDINGS,
)


from engine.runtime.change_requests import change_request
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast

from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_BRANCH_REF,
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.graph_runtime_langgraph import (
    GraphWorkflow,
    WorkflowInput,
    CHEAPEST_TIER,
    DEFAULT_TIER,
    ELEVATED_TIER,
    State,
    TerminalMcpServer,
    configured_agents,
    graph_workflow,
    on_runner,
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
    REVIEW_BRANCH_INPUT, REVIEW_PR_INPUT, REVIEW_REF_INPUT,
    ForgeMode, StepCompleted, WorkState, forge_mode, start_state,
)
from engine.graph_runtime.inputs import (
    LEAST_UTILIZED, ROUND_ROBIN, mode_input, state_input,
)
from engine.graph_runtime_langgraph.executions import NodeExecution
from engine.ports import ApprovalHandler, WorkspaceProvider
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from langgraph_acp import ACPAgentRegistry


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
SPEC = "spec"
REFUTE = "refute"
INVALID_IMPACT_LEVEL = "impact_level must be Green, Orange, or Red"
INVALID_IMPACT_RATIONALE = "impact_rationale must be non-empty"
INVALID_PR_URL = "pr_url must be a pull request or merge request URL"

#: Where the findings a person chose at triage are kept for implementation.
FIX = "fix"

#: The creation inputs naming the change a run started in review looks at.
REF_INPUT, PR_INPUT, BRANCH_INPUT = REVIEW_REF_INPUT, REVIEW_PR_INPUT, REVIEW_BRANCH_INPUT

#: Codex, Claude and OpenCode as `engine.toml` configures them, reached
#: through ACP, and any agent added with `oe agent add`. Steps name a model
#: tier; these resolve it from `[runners.<name>.models]` when the step runs.
AGENTS = configured_agents()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RunnerInput:
    """Resolve the stage's runner per invocation, its model tier and MCP identity."""

    graph_node_runner_input = "implementation_runner"
    model_tier = DEFAULT_TIER

    def _for_runner(self, runner: str) -> ACPNode:
        return on_runner(cast(ACPNode, self), runner, self._model_tier())

    def _model_tier(self) -> str:
        return self.model_tier


class InputImplementationNode(_RunnerInput, ACPNode):
    pass


class InputNameNode(_RunnerInput, NameNode):
    model_tier = CHEAPEST_TIER


class InputImpactAnalysisNode(_RunnerInput, ACPNode):
    """Keep a validated impact rating and its evidence in checkpoint state."""

    graph_node_runner_input = "review_runner"


def _validate_impact_analysis(event: StepCompleted) -> None:
    outputs = {output.name: output.value for output in event.outputs}
    if outputs.get("impact_level") not in ("Green", "Orange", "Red"):
        raise ValueError(INVALID_IMPACT_LEVEL)
    rationale = outputs.get("impact_rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError(INVALID_IMPACT_RATIONALE)


class InputReviewNode(_RunnerInput, ReviewNode):
    graph_node_runner_input = "review_runner"

    def _model_tier(self) -> str:
        return ELEVATED_TIER if self.facet == "security" else DEFAULT_TIER


class _RerankerTools(TerminalMcpServer):
    """The reranker's tools, without `add_comment` in a run that keeps its findings.

    `KEEP_FINDINGS` asks it not to post, so the tool is withheld by the server
    rather than only by the prompt.
    """

    def __call__(
        self,
        state: Mapping[str, Any],
        execution: NodeExecution,
        approve: ApprovalHandler,
    ) -> Any:
        return TerminalMcpServer.__call__(self.for_state(state), state, execution, approve)

    def for_state(self, state: Mapping[str, Any]) -> TerminalMcpServer:
        if not _keeping_findings(state):
            return self
        return replace(self, repository_tools=tuple(
            name for name in self.repository_tools if name != "add_comment"
        ))


class InputRefuteNode(_RunnerInput, RerankerNode):
    pass


class InputRerankerNode(_RunnerInput, RerankerNode):
    async def __call__(self, state: Mapping[str, Any]) -> dict[str, object]:
        update = await super().__call__(state)
        return {**update, "review_rounds": state.get("review_rounds", 0) + 1}


def _cross_reviewer(runner: str) -> str:
    """The other built-in provider; Claude for an added agent, as for Codex."""
    return "codex" if runner == "claude" else "claude"


def _review_node_name(facet_id: str) -> str:
    return f"review-{facet_id}"


def _enabled(state: Mapping[str, Any], name: str) -> bool:
    return str(state.get("inputs", {}).get(name, "false")).lower() == "true"


class ReviewWorkspaceNode(WorkspaceNode):
    async def __call__(self, state: Mapping[str, Any]) -> dict[str, object]:
        inputs = dict(state.get("inputs") or {})
        if _reviewing(state) and not inputs.get(REF_INPUT):
            if inputs.get(PR_INPUT):
                target = change_request(str(inputs[PR_INPUT]))
                if target is None:
                    raise ValueError(INVALID_PR_URL)
                path = f"pull/{target.number}/head" if target.kind == "pull" else f"merge-requests/{target.number}/head"
                inputs[REF_INPUT] = f"origin/refs/{path}"
            else:
                inputs[REF_INPUT] = inputs.get(BRANCH_INPUT) or "HEAD"
        updated = {**state, "inputs": inputs}
        return {**await super().__call__(updated), "inputs": inputs}


def _reviewing(state: Mapping[str, Any]) -> bool:
    """Whether this run started in review, at a change it did not make."""
    inputs = state.get("inputs") or {}
    return (start_state(inputs) is WorkState.REVIEW
            or bool(inputs.get(REF_INPUT) or inputs.get(PR_INPUT)))


def _keeping_findings(state: Mapping[str, Any]) -> bool:
    """Whether the surviving findings are left for a person at triage."""
    return _reviewing(state)


def _pr_url(state: Mapping[str, Any]) -> str:
    """The run's pull request: the one it opened, or the one it was given."""
    inputs = state.get("inputs")
    given = inputs.get(PR_INPUT, "") if isinstance(inputs, Mapping) else ""
    return str(state.get("pr_url") or given or "")


def _push_to(state: Mapping[str, Any]) -> str:
    """Where a fix to a change this run was given goes: that change's branch.

    The workspace is checked out on a branch of its own, so a plain push would
    land beside the pull request rather than on it.
    """
    inputs = state.get("inputs")
    branch = str(inputs.get(BRANCH_INPUT) or "") if isinstance(inputs, Mapping) else ""
    if not (_reviewing(state) and branch and forge_mode(inputs) is ForgeMode.CONNECTED):
        return ""
    return f"The pull request's branch is {branch}: push to it with `git push origin HEAD:{branch}`. "


def _implementation_prompt(state: Mapping[str, Any]) -> str:
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
            f"{UPDATE_CHANGE(state)}{_push_to(state)}Return the branch output too. "
            # Findings a person chose were never posted, so there is nothing to answer.
            f"{'' if _reviewing(state) else ANSWER_REVIEW(state)}"
            "Use fail_step if the findings cannot be addressed.\n\n"
            f"Review findings:\n{json.dumps(findings)}\n\n"
            f"Original task:\n{state.get('task', '')}"
        )
    return IMPLEMENTATION_PROMPT.format(
        publish=PUBLISH_CHANGE(state),
        task=str(state.get("task", "")) + (f"\n\nAgreed specification:\n{state[SPEC]}" if state.get(SPEC) else ""),
    )


def _after_workspace(state: dict[str, Any]) -> str | list[Send]:
    if _reviewing(state):
        return _fan_out_reviews(state)
    return SPEC if _enabled(state, "spec") else NAMING


def _after_reranker(state: dict[str, Any]) -> str:
    if _reviewing(state):
        return TRIAGE
    if state.get(REVIEW) and state.get("review_rounds") == 1:
        return IMPLEMENTATION
    return IMPACT_ANALYSIS


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
    session_config: Mapping[str, Any] | None = None,
    verification: OpenVerify | None = None,
) -> StateGraph:
    """Use declared stage inputs, defaulting to `runner` and the other provider.

    Deployments can replace checkout provisioning, agents and session settings,
    and opt into Open Verify by supplying its installed CLI and evidence uploader.
    """
    # LangGraph accepts an Annotated root channel; its constructor typing expects a class.
    builder: Any = StateGraph(cast(Any, State))
    builder.add_node(
        WORKSPACE,
        ReviewWorkspaceNode(
            provider=workspace_provider
            or GitWorktreeWorkspaceProvider(DEFAULT_ROOT_DIRECTORY),
            base_ref=BASE_REF,
            ref_input=REF_INPUT,
        ),
    )
    builder.add_node(SPEC, InputImplementationNode(
        agent=runner, registry=agents, cwd=checkout,
        prompt=lambda state: (
            "Read the repository and write a concise specification with scope, acceptance "
            "criteria and a test plan. Do not edit files or publish anything. "
            "Call complete_step with the specification in your summary.\n\n"
            + str(state.get("task", ""))
        ),
        mcp_server_bindings=(TerminalMcpServer(step_id=SPEC, agent_id=runner),),
        output_key=SPEC, graph_node_name="Specification", graph_node_group=WorkState.PLANNING,
        session_config=session_config,
    ))
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
                    required_outputs=("pr_url", "branch"),
                    repository_tools=(
                        "git_subcommand", "open_pull_request",
                        "view_change_request", "add_comment",
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

    reviewer = _cross_reviewer(runner)
    for facet in REVIEW_FACETS:
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
                    task=str(state.get("task", "")) + (f"\n\nSpecification:\n{state[SPEC]}" if state.get(SPEC) else ""),
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
                session_config=session_config,
            ),
        )

    builder.add_node(REFUTE, InputRefuteNode(
        agent=runner, registry=agents, cwd=checkout,
        prompt=lambda state: (
            "Try to refute every finding below by reading the code and checking the claimed "
            "failure. Drop disproven, duplicate or non-actionable claims; explain why each "
            "survivor stands. Inspect only: do not edit, commit, push or post comments. "
            "Call complete_step with a findings JSON array, preserving agent and facet.\n\n"
            + json.dumps([finding for facet in REVIEW_FACETS
                          for finding in cast(list[dict[str, Any]], state.get(_review_node_name(facet.id), []))])
        ),
        mcp_server_bindings=(TerminalMcpServer(
            step_id=REFUTE, agent_id=runner, required_outputs=("findings",),
            validate_completion=RerankerNode.validate_completion,
        ),),
        output_key=REFUTE, graph_node_name="Refute findings", session_config=session_config,
    ))
    # ---- reranker: squash noise and post comments ---------------------------

    def _reranker_prompt(state: Mapping[str, Any]) -> str:
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
            findings_sections=(f"Refuted findings:\n{json.dumps(state.get(REFUTE, []))}\n\n"
                               if _enabled(state, "adversarial") else "".join(sections)),
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
        [SPEC, NAMING, *(_review_node_name(f.id) for f in REVIEW_FACETS)],
    )
    builder.add_edge(SPEC, NAMING)
    builder.add_edge(NAMING, IMPLEMENTATION)

    builder.add_edge(IMPLEMENTATION, CI_CHECK)
    builder.add_conditional_edges(
        CI_CHECK, _after_ci,
        [IMPLEMENTATION, *(_review_node_name(f.id) for f in REVIEW_FACETS)],
    )

    # Fan-in: every facet converges on the reranker.
    for facet in REVIEW_FACETS:
        builder.add_conditional_edges(
            _review_node_name(facet.id),
            lambda state: REFUTE if _enabled(state, "adversarial") else RERANKER,
            [REFUTE, RERANKER],
        )
    builder.add_edge(REFUTE, RERANKER)

    builder.add_conditional_edges(
        RERANKER, _after_reranker, [IMPLEMENTATION, IMPACT_ANALYSIS, TRIAGE],
    )
    builder.add_conditional_edges(TRIAGE, _after_triage, [IMPLEMENTATION, END])
    if verification is not None:
        builder.add_node(VERIFICATION, verification)
        builder.add_edge(IMPACT_ANALYSIS, VERIFICATION)
        builder.add_edge(VERIFICATION, HUMAN_REVIEW)
    else:
        builder.add_edge(IMPACT_ANALYSIS, HUMAN_REVIEW)
    builder.add_edge(HUMAN_REVIEW, END)
    return builder


#: Runner choices for the implementation and review stages.  An agent added
#: with `oe agent add` is offered beside these by the process serving it,
#: and joins the policies when installed and signed in on the backend.
DEFAULT_RUNNER = "round-robin"
DEFAULT_REVIEW_RUNNER = "round-robin"
RUNNER_CHOICES = tuple(dict.fromkeys(
    runner for runner in ("codex", "claude", "opencode", DEFAULT_RUNNER)
    if runner not in (LEAST_UTILIZED, ROUND_ROBIN)
))
#: What the runner dropdowns offer: a runner, or a policy that picks one per run.
RUNNER_INPUT_CHOICES = (*RUNNER_CHOICES, LEAST_UTILIZED, ROUND_ROBIN)
#: What this workflow was called when the runner was part of its id, before the
#: stages became creation inputs. WorkOrders started then remember one of these,
#: so they are retired rather than dropped and go on opening as this workflow.
PREVIOUS_IDS = ("implementation-review-codex", "implementation-review-claude")
# The loader rebuilds one default graph with deployment session configuration.
RUNNERS = (DEFAULT_RUNNER,)


def graph_for(
    runner: str,
    *,
    workspace_provider: WorkspaceProvider | None = None,
    agents: ACPAgentRegistry = AGENTS,
    session_config: Mapping[str, Any] | None = None,
    verification: OpenVerify | None = None,
) -> GraphWorkflow:
    """Build the workflow with the requested initial implementation runner."""
    return graph_workflow(
        pipeline(
            # Nodes need a concrete construction default; submission resolves
            # policies into the stage inputs before any node starts.
            "codex" if runner in (LEAST_UTILIZED, ROUND_ROBIN) else runner,
            workspace_provider=workspace_provider,
            agents=agents,
            session_config=session_config,
            verification=verification,
        ),
        id="implementation-review-rerank",
        name="Implementation review rerank",
        description="Implement, check CI, review parallel facets, rerank, fix once, and ask for human review.",
        previous_ids=PREVIOUS_IDS,
        inputs=(
            WorkflowInput("instruction", "Instruction"),
            WorkflowInput(
                "implementation_runner", "Implementation runner",
                default=runner, required=True, choices=RUNNER_INPUT_CHOICES,
            ),
            WorkflowInput(
                "review_runner", "Review runner",
                default=DEFAULT_REVIEW_RUNNER or _cross_reviewer(runner),
                required=True, choices=RUNNER_INPUT_CHOICES,
            ),
            *(WorkflowInput(name, label, default="false", choices=("false", "true"))
              for name, label in (("spec", "Write a specification first"),
                                  ("adversarial", "Refute review findings"))),
            mode_input(),
            state_input(WorkState.PLANNING, WorkState.REVIEW),
            WorkflowInput(REF_INPUT, "Ref to review"),
            WorkflowInput(PR_INPUT, "Pull request to review"),
            WorkflowInput(BRANCH_INPUT, "Pull request branch"),
        ),
    )


workflow = graph_for(DEFAULT_RUNNER)

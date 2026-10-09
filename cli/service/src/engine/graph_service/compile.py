"""A `GraphSpec`, compiled to a LangGraph `StateGraph` of the engine's components.

    checkout (WorkspaceNode) -> entry nodes -> ... -> END

    agent   ACPNode, one langgraph-acp session per execution; with `tools` or
            `outputs`, a TerminalMcpServer the agent finishes through
    human   HumanReviewNode; with `choose:`, TriageNode
    ci      CICheck

A parallel node becomes one LangGraph node per branch (`review-security`,
`review-bugs`, ...), started together and joined by whatever follows. A route
becomes a conditional edge whose router evaluates the `when` expressions over
the run's state; a route out of a parallel node routes from a join node, so it
is decided once, after every branch has finished.

What the run's state holds, beyond the built-ins:

    <node key>            the node's output: the agent's final message, or for a
                          node with outputs {summary, runner, item?, <outputs>}
    _visits.<node key>    how many times that node has finished
    _runner.<node key>    the runner its latest execution used

The graph's id is the version's id, so a run pins exactly what it started.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from engine.domain import StepCompleted
from engine.graph_runtime.inputs import RUNNER_POLICIES, WorkflowInput
from engine.graph_runtime_langgraph import (
    GraphWorkflow,
    HumanReviewNode,
    State,
    TerminalMcpServer,
    WorkspaceNode,
    current_execution,
    graph_workflow,
)
from engine.graph_runtime_langgraph.acp import TerminalEvent
from engine.graph_runtime_langgraph.components import ACPNode, CICheck, TriageNode, checkout
from engine.graph_runtime_langgraph.components.findings import parse_findings
from engine.ports import WorkspaceProvider
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry

from engine.graph_service.expressions import parse, template
from engine.graph_service.language import (
    END as FLOW_END,
    STAGE_GROUPS,
    GraphSpec,
    NodeSpec,
    OutputSpec,
    Route,
    RunnerRule,
)

#: What `model: default` and `model: elevated` mean when a backend says nothing.
DEFAULT_MODEL_TIERS: Mapping[str, Mapping[str, str]] = {
    "claude": {"default": "sonnet", "elevated": "opus"},
    "codex": {"default": "gpt-5.6-terra", "elevated": "gpt-5.6-sol"},
}

Groups = Mapping[str, tuple[tuple[str, ...], bool]]
"""Node id -> (the LangGraph node keys it compiled to, whether it is parallel)."""


def visits_key(key: str) -> str:
    return f"_visits.{key}"


def runner_key(key: str) -> str:
    return f"_runner.{key}"


def environment(state: Mapping[str, Any], groups: Groups, item: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The names an expression can read, from a run's state."""
    outputs: dict[str, Any] = {}
    visits: dict[str, int] = {}
    for node_id, (keys, parallel) in groups.items():
        outputs[node_id] = [state[key] for key in keys if key in state] if parallel else state.get(keys[0])
        visits[node_id] = max((int(state.get(visits_key(key)) or 0) for key in keys), default=0)
    return {
        "instruction": state.get("task", ""),
        "repository": state.get("repository", ""),
        "inputs": state.get("inputs") or {},
        "outputs": outputs,
        "visits": visits,
        "item": item,
    }


def compile_graph(
    spec: GraphSpec,
    *,
    version_id: str,
    number: int,
    workspace_provider: WorkspaceProvider,
    registry: ACPAgentRegistry,
    session_config: Mapping[str, object] | None = None,
    model_tiers: Mapping[str, Mapping[str, str]] | None = None,
    agent_models: Mapping[str, str] | None = None,
    default_base_ref: str = "origin/HEAD",
) -> GraphWorkflow:
    groups: Groups = {node.id: (node.keys, bool(node.parallel)) for node in spec.nodes}
    tiers = {**DEFAULT_MODEL_TIERS, **(model_tiers or {})}
    runners = tuple(registry.names)
    builder: Any = StateGraph(State)
    builder.add_node(
        spec.checkout,
        WorkspaceNode(
            workspace_provider,
            base_ref=spec.base_ref or default_base_ref,
            repository=spec.repository,
            ref_input=spec.ref_input,
        ),
    )
    for node in spec.nodes:
        for key, branch in zip(node.keys, node.parallel or (None,)):
            builder.add_node(key, _node(
                node, key, branch.item if branch else None, groups,
                registry=registry, runners=runners, tiers=tiers, session_config=session_config,
                agent_models={} if agent_models is None else agent_models,
            ))

    def keys_of(name: str) -> list[str]:
        if name == FLOW_END:
            return [END]
        return list(groups[name][0])

    builder.add_edge(START, spec.checkout)
    for entry in spec.entries:
        for key in keys_of(entry):
            builder.add_edge(spec.checkout, key)
    joined: set[str] = set()
    for rule in spec.flow:
        if rule.sources == ("start",):
            continue
        sources = [key for source in rule.sources for key in keys_of(source)]
        if rule.routes:
            origin = rule.sources[0]
            if groups[origin][1]:
                join = f"{origin}-join"
                if join not in joined:
                    builder.add_node(join, JoinNode(graph_node_name=f"{origin} done"))
                    builder.add_edge(sources, join)
                    joined.add(join)
                origin = join
            destinations = sorted(
                {key for route in rule.routes for target in route.targets for key in keys_of(target)}, key=str,
            )
            builder.add_conditional_edges(origin, _router(rule.routes, groups, keys_of), destinations)
            continue
        for target in rule.targets:
            for destination in keys_of(target):
                if len(sources) > 1:
                    builder.add_edge(sources, destination)
                else:
                    builder.add_edge(sources[0], destination)
    return graph_workflow(
        builder,
        id=version_id,
        name=f"{spec.name} v{number}",
        inputs=tuple(
            WorkflowInput(item.name, item.description or item.name, item.default, item.required, item.choices)
            for item in spec.inputs
        ),
    )


def _router(
    routes: Sequence[Route], groups: Groups, keys_of: Callable[[str], list[str]],
) -> Callable[[Mapping[str, Any]], Any]:
    compiled = [(parse(route.when) if route.when else None, route.targets) for route in routes]

    def route(state: Mapping[str, Any]) -> str | list[str]:
        names = environment(state, groups)
        for condition, targets in compiled:
            if condition is None or condition.evaluate(names):
                destinations = [key for target in targets for key in keys_of(target)]
                return destinations[0] if len(destinations) == 1 else destinations
        return END

    return route


def _node(
    node: NodeSpec,
    key: str,
    item: Mapping[str, Any] | None,
    groups: Groups,
    *,
    registry: ACPAgentRegistry,
    runners: tuple[str, ...],
    tiers: Mapping[str, Mapping[str, str]],
    agent_models: Mapping[str, str],
    session_config: Mapping[str, object] | None,
) -> Any:
    label = node.name or node.id
    if item is not None:
        facet = item.get("facet")
        label = f"{label} ({facet['name'] if isinstance(facet, Mapping) else key.removeprefix(node.id + '-')})"
    common = {
        "graph_node_name": label,
        "graph_node_description": node.description,
        "graph_node_group": STAGE_GROUPS[node.stage],
    }
    if node.kind == "human":
        return GraphHumanNode(state_key=key, **({"prompt": node.prompt} if node.prompt else {}), **common)
    if node.kind == "triage":
        return GraphTriageNode(
            findings_key=f"_choose.{key}", output_key=key, state_key=key, choose=node.choose,
            groups=groups, **common,
        )
    if node.kind == "ci":
        return GraphCINode(output_key=key, state_key=key, **common)
    assert node.runner is not None
    default = _default_runner(node.runner, runners)
    prompt_template = template(node.prompt)
    instructions = _completion_instructions(node.outputs) if node.structured else ""

    def prompt(state: Mapping[str, object]) -> str:
        return prompt_template.render(environment(state, groups, item)) + instructions

    bindings: tuple[Any, ...] = ()
    if node.structured:
        bindings = (TerminalMcpServer(
            step_id=key,
            agent_id=default,
            required_outputs=tuple(output.name for output in node.outputs if output.required),
            repository_tools=node.tools,
            validate_completion=_validator(node.outputs),
        ),)
    facet = item.get("facet") if item else None
    return GraphAgentNode(
        agent=default,
        prompt=prompt,
        registry=registry,
        cwd=checkout,
        output_key=key,
        mcp_server_bindings=bindings,
        session_config=session_config,
        graph_node_always_open=node.always_open,
        state_key=key,
        rule=node.runner,
        model_template=node.model,
        outputs=node.outputs,
        item=dict(item) if item else None,
        facet_id=facet["id"] if isinstance(facet, Mapping) else node.id,
        groups=groups,
        tiers=tiers,
        agent_models=agent_models,
        runners=runners,
        **common,
    )


def _default_runner(rule: RunnerRule, runners: Sequence[str]) -> str:
    if rule.kind == "literal":
        return rule.value
    if rule.choices:
        return rule.choices[0]
    return runners[0] if runners else "claude"


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphAgentNode(ACPNode):
    """An agent node whose runner, model and output shape come from the graph."""

    state_key: str
    rule: RunnerRule
    model_template: str = ""
    outputs: tuple[OutputSpec, ...] = ()
    item: Mapping[str, Any] | None = None
    facet_id: str = ""
    groups: Groups = field(default_factory=dict)
    tiers: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    agent_models: Mapping[str, str] = field(default_factory=dict)
    """An added agent's own model, for a node that names none or a tier it has no entry for."""
    runners: tuple[str, ...] = ()

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        execution = current_execution()
        record = await execution.runtime.store.run(execution.run_id)
        override = record.runner_overrides.get(execution.node_id) if record else None
        runner = override or self.resolve_runner(state)
        config = dict(self.session_config or {})
        if model := self.model_for(state, runner):
            config["model"] = model
        node = replace(
            self,
            agent=runner,
            session_config=config or None,
            mcp_server_bindings=tuple(
                replace(binding, agent_id=runner) if isinstance(binding, TerminalMcpServer) else binding
                for binding in self.mcp_server_bindings
            ),
        )
        update = await ACPNode.__call__(node, state)
        return {
            **update,
            visits_key(self.state_key): int(state.get(visits_key(self.state_key)) or 0) + 1,
            runner_key(self.state_key): runner,
        }

    def resolve_runner(self, state: Mapping[str, object]) -> str:
        rule = self.rule
        pool = rule.choices or self.runners
        if rule.kind == "literal":
            return rule.value
        if rule.kind == "input":
            inputs = state.get("inputs")
            chosen = inputs.get(rule.value) if isinstance(inputs, Mapping) else None
            if isinstance(chosen, str) and chosen and chosen not in RUNNER_POLICIES:
                return chosen
            return self.agent
        other = next(
            (str(state[runner_key(key)]) for key in self.groups[rule.value][0] if state.get(runner_key(key))),
            None,
        )
        if rule.kind == "same":
            return other or (pool[0] if pool else self.agent)
        return next((runner for runner in pool if runner != other), self.agent)

    def model_for(self, state: Mapping[str, object], runner: str) -> str:
        fallback = self.agent_models.get(runner, "")
        if not self.model_template:
            return fallback
        chosen = template(self.model_template).render(environment(state, self.groups, self.item)).strip()
        named = {tier for tiers in self.tiers.values() for tier in tiers}
        return self.tiers.get(runner, {}).get(chosen, fallback if chosen in named else chosen)

    def _terminal_update(self, event: TerminalEvent) -> dict[str, object]:
        base = ACPNode._terminal_update(self, event)
        value: dict[str, object] = {"summary": base.pop(self.state_key, ""), "runner": self.agent}
        if self.item is not None:
            value["item"] = dict(self.item)
        for output in self.outputs:
            if output.name not in base:
                continue
            raw = base[output.name]
            if output.type == "findings":
                findings = parse_findings(raw, require_lineage=output.lineage)
                if not output.lineage:
                    findings = [replace(finding, agent=self.agent, facet=self.facet_id) for finding in findings]
                raw = [finding.to_dict() for finding in findings]
            value[output.name] = raw
        update: dict[str, object] = {self.state_key: value}
        if "pr_url" in base:
            # Where CICheck and the forge tools look for the change.
            update["pr_url"] = base["pr_url"]
        return update


@dataclass(frozen=True, slots=True)
class GraphHumanNode(HumanReviewNode):
    state_key: str = ""

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        update = await HumanReviewNode.__call__(self, state)
        return {
            **update,
            self.state_key: {"decision": update.get("decision"), "note": update.get("decisionNote", "")},
            visits_key(self.state_key): int(state.get(visits_key(self.state_key)) or 0) + 1,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class GraphTriageNode(TriageNode):
    state_key: str
    choose: str
    groups: Groups = field(default_factory=dict)

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        offered = parse(self.choose).evaluate(environment(state, self.groups))
        offered = offered if isinstance(offered, list) else []
        update = await TriageNode.__call__(self, {**state, self.findings_key: offered})
        return {
            **update,
            self.findings_key: offered,
            visits_key(self.state_key): int(state.get(visits_key(self.state_key)) or 0) + 1,
        }


@dataclass(frozen=True, slots=True)
class GraphCINode(CICheck):
    state_key: str = ""

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        update = await CICheck.__call__(self, state)
        return {**update, visits_key(self.state_key): int(state.get(visits_key(self.state_key)) or 0) + 1}


@dataclass(frozen=True, slots=True)
class JoinNode:
    """Where a route out of a parallel node waits for every branch."""

    graph_node_name: str = "join"
    graph_node_kind: str = "join"
    graph_node_show_in_sidebar: bool = False

    async def __call__(self, _state: Mapping[str, object]) -> dict[str, object]:
        return {}


def _completion_instructions(outputs: Sequence[OutputSpec]) -> str:
    lines = ["", "", "When you are finished, call the complete_step tool with a short summary"]
    if not outputs:
        lines[-1] += "."
        return "\n".join(lines)
    lines[-1] += " and these outputs:"
    for output in outputs:
        detail = {
            "findings": (
                "a JSON array of findings, each {\"tagline\": 1-2 plain-language lines, "
                "\"description\": 1-3 lines on what it is and why it matters, \"file\"?: path, "
                "\"line\"?: number, \"severity\"?: \"high\"|\"medium\"|\"low\"}; [] when there are none"
                + ("; keep each finding's agent and facet unchanged" if output.lineage else "")
            ),
            "list": "a JSON array",
            "object": "a JSON object",
        }.get(output.type, output.type)
        if output.enum:
            detail = "exactly one of " + ", ".join(output.enum)
        required = " (required)" if output.required else ""
        description = f" -- {output.description}" if output.description else ""
        lines.append(f"- {output.name}{required}: {detail}{description}")
    return "\n".join(lines)


def _validator(outputs: Sequence[OutputSpec]) -> Callable[[StepCompleted], None]:
    """Reject a completion whose outputs do not match the graph, so the agent can correct it."""

    def validate(event: StepCompleted) -> None:
        values = {output.name: output.value for output in event.outputs}
        for output in outputs:
            value = values.get(output.name)
            if value is None or value == "":
                if output.required:
                    raise ValueError(f"output {output.name} is required")
                continue
            if output.type == "findings":
                parse_findings(value, require_lineage=output.lineage)
            elif output.enum and value not in output.enum:
                raise ValueError(f"output {output.name} must be one of {', '.join(output.enum)}")
            elif not _typed(value, output.type):
                raise ValueError(f"output {output.name} must be a {output.type}")

    return validate


def _typed(value: object, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "list":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, Mapping)
    return True


__all__ = ["DEFAULT_MODEL_TIERS", "compile_graph", "environment"]

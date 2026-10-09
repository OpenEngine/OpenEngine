"""The YAML graph language: parsing, the graph it declares, and what makes one valid.

See `cli/GRAPH_LANGUAGE.md` for the language itself. This module turns a
decoded document into a `GraphSpec` -- the graph as data -- reporting every
problem at once, each with the path it was found at. `compile.py` turns a
`GraphSpec` into LangGraph.

Steps are written in three stage sections -- `plan`, `implementation`,
`review` -- and run in that order, each section's steps in the order written.
A step's kind comes from its keys:

    agent:      an ACP session      (+ tools, outputs, runner rules)
    human:      a person decides    (`choose:` lets them pick findings instead)
    ci:         wait for CI on the change
    otherwise:  the checkout (`base_ref`, `ref`); first if present, else implicit

and an agent step with `facets:` runs once per facet, in parallel. `flow` holds only what the written order cannot say: a step given an
edge or route there leaves the written order at that point. Cycles must pass
through a route, so every loop has a way out; every step must be reachable and
able to finish.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from engine.graph_runtime_langgraph.components.findings import REVIEW_FACETS
from engine.runtime.terminal_mcp import REPOSITORY_TOOL_NAMES

from engine.graph_service.expressions import Expression, ExpressionError, Template, parse, template
from engine.graph_service.schema import (
    API_VERSION, FIELDS, FIELD_RULES, KIND, MIN_INTERVAL_SECONDS, OUTPUT_TYPES, RUNNER_POLICIES,
    SECTIONS, _UNITS,
)

WORKSPACE_NODE = "workspace"
START, END = "start", "end"
STAGES = ("plan", "implement", "review")
SECTION_OF = {stage: section for section, stage in SECTIONS.items()}
STAGE_GROUPS = {"plan": "Planning", "implement": "Implementation", "review": "Review"}
INSTRUCTION = "instruction"
TOOLS = REPOSITORY_TOOL_NAMES
FACETS = {
    facet.id: {"id": facet.id, "name": facet.name, "focus": facet.focus, "elevated": facet.elevated}
    for facet in REVIEW_FACETS
}

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,30}$")
_DURATION = re.compile(r"^(\d+)\s*(s|m|h|d)$")
_ARROW = re.compile(r"^\s*(\[[^\]]*\]|[^\s\[\]>-]+)\s*->\s*(\[[^\]]*\]|[^\s\[\]>-]+)\s*$")


@dataclass(frozen=True)
class Problem:
    path: str
    message: str

    def json(self) -> dict[str, str]:
        return {"path": self.path, "message": self.message}


class GraphError(ValueError):
    """A graph that cannot be registered, with every problem found."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(f"{p.path}: {p.message}" for p in self.problems))


@dataclass(frozen=True)
class InputSpec:
    name: str
    description: str = ""
    required: bool = False
    default: str = ""
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunnerRule:
    """Which runner an agent node uses, decided when it runs.

    `literal`: that runner. `input`: whatever the named input says. `not`: a
    runner other than the one that last ran `node`. `same`: the one that did.
    """

    kind: str
    value: str
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutputSpec:
    name: str
    type: str = "string"
    enum: tuple[str, ...] = ()
    required: bool = False
    lineage: bool = False
    """Findings that must keep the reviewer and facet they came with."""
    description: str = ""


@dataclass(frozen=True)
class Branch:
    """One execution of a parallel node: its id suffix and its `item`."""

    id: str
    item: Mapping[str, Any]


@dataclass(frozen=True)
class NodeSpec:
    id: str
    kind: str
    """`agent`, `human`, `triage` or `ci`."""
    stage: str
    """`plan`, `implement` or `review`: the section it is written in."""
    name: str = ""
    description: str = ""
    prompt: str = ""
    runner: RunnerRule | None = None
    model: str = ""
    tools: tuple[str, ...] = ()
    outputs: tuple[OutputSpec, ...] = ()
    always_open: bool = False
    parallel: tuple[Branch, ...] = ()
    choose: str = ""
    """For `triage`: the expression naming the findings a person picks from."""

    @property
    def structured(self) -> bool:
        return bool(self.outputs or self.tools)

    @property
    def keys(self) -> tuple[str, ...]:
        """The LangGraph nodes this compiles to: one, or one per parallel branch."""
        return tuple(f"{self.id}-{branch.id}" for branch in self.parallel) or (self.id,)

    @property
    def path(self) -> str:
        return f"{SECTION_OF[self.stage]}.{self.id}"


@dataclass(frozen=True)
class Route:
    when: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class FlowRule:
    sources: tuple[str, ...]
    targets: tuple[str, ...] = ()
    routes: tuple[Route, ...] = ()
    join: bool = False


@dataclass(frozen=True)
class LoopDefaults:
    instruction: str = ""
    interval_seconds: int | None = None


@dataclass(frozen=True)
class GraphSpec:
    name: str
    description: str
    instruction: str
    """How the instruction is described to a person, from `inputs.instruction`."""
    inputs: tuple[InputSpec, ...]
    base_ref: str
    ref_input: str
    nodes: tuple[NodeSpec, ...]
    flow: tuple[FlowRule, ...]
    loop: LoopDefaults = field(default_factory=LoopDefaults)
    repository: str = ""
    checkout: str = WORKSPACE_NODE
    """The checkout's node id: the step that has neither agent, human nor ci."""

    def node(self, node_id: str) -> NodeSpec | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    @property
    def entries(self) -> tuple[str, ...]:
        explicit = tuple(t for rule in self.flow if rule.sources == (START,) for t in rule.targets)
        if explicit:
            return explicit
        targets = {t for rule in self.flow for t in (*rule.targets, *(t for r in rule.routes for t in r.targets))}
        return tuple(node.id for node in self.nodes if node.id not in targets)

    def edges(self) -> list[tuple[str, str, bool]]:
        """Every `(source, target, conditional)` between node ids, start and end."""
        found = [(START, entry, False) for entry in self.entries]
        for rule in self.flow:
            if rule.sources == (START,):
                continue
            for source in rule.sources:
                for target in rule.targets:
                    found.append((source, target, False))
                for route in rule.routes:
                    for target in route.targets:
                        found.append((source, target, True))
        return found

    def json(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.json(), sort_keys=True, default=str).encode()).hexdigest()


def parse_duration(value: object) -> int:
    """`90s`, `30m`, `6h`, `1d` or a number of seconds, at least a minute."""
    if isinstance(value, bool):
        raise ValueError("a duration is a number of seconds or like 30m, 6h, 1d")
    if isinstance(value, int):
        seconds = value
    elif isinstance(value, str) and (match := _DURATION.match(value.strip().lower())):
        seconds = int(match.group(1)) * _UNITS[match.group(2)]
    else:
        raise ValueError("a duration is a number of seconds or like 30m, 6h, 1d")
    if seconds < MIN_INTERVAL_SECONDS:
        raise ValueError(f"a cadence must be at least {MIN_INTERVAL_SECONDS} seconds")
    return seconds


def parse_graph(raw: object, *, runners: Sequence[str] | None = None) -> GraphSpec:
    """Read a decoded YAML graph, collecting every problem rather than the first.

    `runners` is what the backend can launch; a graph naming another is refused
    here, at registration, rather than when a run reaches that node.
    """
    problems: list[Problem] = []

    def problem(path: str, message: str) -> None:
        problems.append(Problem(path, message))

    if not isinstance(raw, Mapping):
        raise GraphError([Problem("$", "a graph must be a mapping")])
    _known(raw, FIELDS["graph"], "", problem)
    graph_fields = FIELD_RULES["graph"]
    graph_fields["apiVersion"].read(raw, "apiVersion", "apiVersion", problem)
    graph_fields["kind"].read(raw, "kind", "kind", problem)
    name = graph_fields["name"].read(raw, "name", "name", problem)
    description = _text(raw, "description", "description", problem)
    repository = _text(raw, "repository", "repository", problem)
    instruction, inputs = _inputs(raw.get("inputs"), problem)
    input_names = {item.name for item in inputs}

    steps = _steps(raw, problem)
    checkout, base_ref, ref_input = _checkout(steps, input_names, problem)
    nodes = _nodes([step for step in steps if step[0] != checkout], inputs, runners, problem)
    ids = {node.id for node in nodes}
    keys = [checkout, *(key for node in nodes for key in node.keys)]
    for key in sorted({key for key in keys if keys.count(key) > 1}):
        problem(next(node.path for node in nodes if key in node.keys),
                f"{key!r} is used twice; rename a step")
    flow = _flow(raw.get("flow"), ids, problem)
    flow = (*_written_order([node.id for node in nodes], flow), *flow)
    loop = _loop(raw.get("loop"), problem)
    spec = GraphSpec(
        name=name, description=description, instruction=instruction, inputs=inputs,
        base_ref=base_ref, ref_input=ref_input, nodes=nodes, flow=flow, loop=loop,
        repository=repository, checkout=checkout,
    )
    if nodes and not problems:
        _structure(spec, problem)
    if not problems:
        _references(spec, input_names, problem)
    if problems:
        raise GraphError(problems)
    return spec


# --- sections ---------------------------------------------------------------


def _inputs(raw: object, problem: Any) -> tuple[str, tuple[InputSpec, ...]]:
    if raw is None:
        return "", ()
    if not isinstance(raw, Mapping):
        problem("inputs", "must be a mapping of input name to {description, required, default, choices}")
        return "", ()
    instruction = ""
    found: list[InputSpec] = []
    for name, value in raw.items():
        where = f"inputs.{name}"
        value = value or {}
        if not isinstance(value, Mapping):
            problem(where, "must be a mapping")
            continue
        _known(value, FIELDS["input"], f"{where}.", problem)
        if name == INSTRUCTION:
            # Built in: every run is given one. Declaring it only describes it.
            instruction = str(value.get("description") or "").strip()
            continue
        if not isinstance(name, str) or not _IDENTIFIER.match(name):
            problem(where, "an input name is letters, digits and '_', starting with a letter")
            continue
        required = FIELD_RULES["input"]["required"].read(
            value, "required", f"{where}.required", problem)
        default = value.get("default", "")
        default = "" if default is None else default
        if not isinstance(default, (str, int, float)) or isinstance(default, bool):
            problem(f"{where}.default", "must be text")
            default = ""
        choices = value.get("choices", [])
        if not isinstance(choices, list) or not all(isinstance(choice, str) for choice in choices):
            problem(f"{where}.choices", "must be a list of text")
            choices = []
        if default and choices and str(default) not in choices:
            problem(f"{where}.default", "must be one of its choices")
        found.append(InputSpec(
            name, str(value.get("description") or "").strip(), required, str(default), tuple(choices),
        ))
    return instruction, tuple(found)


Step = tuple[str, str, object]
"""A step as written: its id, its stage, and its value."""


def _steps(raw: Mapping[str, Any], problem: Any) -> list[Step]:
    """Every step, in the order it runs: section by section, as written."""
    steps: list[Step] = []
    seen: dict[str, str] = {}
    for section, stage in SECTIONS.items():
        value = raw.get(section)
        if value is None:
            continue
        if not isinstance(value, Mapping) or not value:
            problem(section, "must be a mapping of step name to step")
            continue
        for step_id, step in value.items():
            where = f"{section}.{step_id}"
            if not isinstance(step_id, str) or not _IDENTIFIER.match(step_id) or step_id in (START, END):
                problem(where, "a step name is letters, digits and '_', starting with a letter, "
                        "and not start or end")
                continue
            if step_id in seen:
                problem(where, f"{step_id!r} is already a step in {seen[step_id]}")
                continue
            seen[step_id] = section
            steps.append((step_id, stage, step))
    if not any(_kind(step) for _, _, step in steps):
        problem("plan", "a graph needs at least one step in plan, implementation or review")
    return steps


def _kind(value: object) -> str:
    """`agent`, `human` or `ci`; "" for the checkout, "?" for a step with several."""
    if not isinstance(value, Mapping):
        return ""
    kinds = [key for key in ("agent", "human", "ci") if key in value]
    return kinds[0] if len(kinds) == 1 else "?" if kinds else ""


def _checkout(steps: Sequence[Step], input_names: set[str], problem: Any) -> tuple[str, str, str]:
    """The checkout step, if one is written: its id, base_ref and ref input."""
    found = [(index, step) for index, step in enumerate(steps) if not _kind(step[2])]
    for index, (step_id, stage, _) in found:
        if index:
            problem(f"{SECTION_OF[stage]}.{step_id}", "the checkout (a step with no agent, human or ci) "
                    "must be the first step")
    named = next((step for step in steps if step[0] == WORKSPACE_NODE and _kind(step[2])), None)
    if named and not found:
        problem(f"{SECTION_OF[named[1]]}.{WORKSPACE_NODE}", "workspace names the checkout; "
                "an agent, human or ci step needs another name")
    if not found or found[0][0]:
        return WORKSPACE_NODE, "", ""
    step_id, stage, value = found[0][1]
    where = f"{SECTION_OF[stage]}.{step_id}"
    value = value or {}
    if not isinstance(value, Mapping):
        problem(where, "a checkout is a mapping such as {base_ref: origin/HEAD}")
        return step_id, "", ""
    _known(value, FIELDS["checkout"], f"{where}.", problem)
    base_ref = _text(value, "base_ref", f"{where}.base_ref", problem)
    ref = _text(value, "ref", f"{where}.ref", problem)
    ref_input = ""
    if ref:
        single = _template(ref, f"{where}.ref", problem)
        path = single.single.references if single and single.single else ()
        if len(path) != 1 or len(path[0]) != 2 or path[0][0] != "inputs":
            problem(f"{where}.ref", "must be exactly ${inputs.NAME}")
        elif path[0][1] not in input_names:
            problem(f"{where}.ref", f"{path[0][1]!r} is not a declared input")
        else:
            ref_input = path[0][1]
    return step_id, base_ref, ref_input


def _nodes(
    steps: Sequence[Step], inputs: Sequence[InputSpec], runners: Sequence[str] | None, problem: Any,
) -> tuple[NodeSpec, ...]:
    found: list[NodeSpec] = []
    agents = {step_id for step_id, _, value in steps if _kind(value) == "agent"}
    for node_id, stage, value in steps:
        where = f"{SECTION_OF[stage]}.{node_id}"
        kind = _kind(value)
        if kind in ("", "?"):
            if kind == "?":
                problem(where, "must have only one of agent, human or ci")
            continue
        assert isinstance(value, Mapping)
        common = {
            "name": str(value.get("name") or "").strip(),
            "description": str(value.get("description") or "").strip(),
        }
        if kind == "agent":
            found.append(_agent(node_id, value, stage, common, inputs, runners, agents, problem))
        elif kind == "human":
            _known(value, FIELDS["human step"], f"{where}.", problem)
            human = value["human"]
            if isinstance(human, str) and human.strip():
                found.append(NodeSpec(node_id, "human", stage, prompt=human.strip(), **common))  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
            elif isinstance(human, Mapping):
                _known(human, FIELDS["human"], f"{where}.human.", problem)
                choose = human.get("choose")
                if choose is not None and not isinstance(choose, str):
                    problem(f"{where}.human.choose", "must be an expression such as outputs.reranker.findings")
                    choose = None
                if choose:
                    try:
                        parse(choose)
                    except ExpressionError as error:
                        problem(f"{where}.human.choose", str(error))
                found.append(NodeSpec(
                    node_id, "triage" if choose else "human", stage,
                    prompt=str(human.get("prompt") or "").strip(), choose=choose or "", **common,  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
                ))
            else:
                problem(f"{where}.human", "the question to ask, or {prompt, choose}")
        else:
            _known(value, FIELDS["ci step"], f"{where}.", problem)
            if value["ci"] is not True:
                problem(f"{where}.ci", "must be true")
            found.append(NodeSpec(node_id, "ci", stage, **common))  # pyright: ignore[reportArgumentType]  # Baseline: see docs/pyright.md
    return tuple(found)


def _agent(
    node_id: str,
    value: Mapping[str, Any],
    stage: str,
    common: Mapping[str, str],
    inputs: Sequence[InputSpec],
    runners: Sequence[str] | None,
    agents: set[str],
    problem: Any,
) -> NodeSpec:
    where = f"{SECTION_OF[stage]}.{node_id}"
    _known(value, FIELDS["agent step"], f"{where}.", problem)
    parallel = _facets(value.get("facets"), f"{where}.facets", problem)
    fields = FIELD_RULES["agent step"]
    prompt = fields["prompt"].read(value, "prompt", f"{where}.prompt", problem)
    runner = _runner(value["agent"], f"{where}.agent", node_id, inputs, runners, agents, problem)
    tools = value.get("tools") or []
    if not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools):
        problem(f"{where}.tools", "must be a list of tool names")
        tools = []
    for index, tool in enumerate(tools):
        if tool not in TOOLS:
            problem(f"{where}.tools[{index}]", f"unknown tool {tool!r}; available: {', '.join(TOOLS)}")
    outputs = _outputs(value.get("outputs"), f"{where}.outputs", problem)
    model = fields["model"].read(value, "model", f"{where}.model", problem)
    if any("model" in branch.item for branch in parallel):
        if model:
            problem(f"{where}.model", "give a model here or on each facet, not both")
        model = "${item.model}"
    steering = fields["steering"].read(value, "steering", f"{where}.steering", problem)
    return NodeSpec(
        node_id, "agent", stage, prompt=prompt.strip(), runner=runner, model=model.strip(),
        tools=tuple(tools), outputs=outputs, always_open=steering == "always-open",
        parallel=parallel, **common,
    )


def _runner(
    raw: object,
    where: str,
    node_id: str,
    inputs: Sequence[InputSpec],
    runners: Sequence[str] | None,
    agents: set[str],
    problem: Any,
) -> RunnerRule | None:
    available = tuple(runners) if runners is not None else None

    def check(name: str, path: str) -> None:
        if available is not None and name not in available:
            problem(path, f"runner {name!r} is not configured on this backend "
                    f"(available: {', '.join(available) or 'none'})")

    if isinstance(raw, str) and raw.strip().startswith("${"):
        parsed = _template(raw.strip(), where, problem)
        path = parsed.single.references if parsed and parsed.single else ()
        if len(path) != 1 or len(path[0]) != 2 or path[0][0] != "inputs":
            problem(where, "a runner from an input is exactly ${inputs.NAME}")
            return None
        declared = next((item for item in inputs if item.name == path[0][1]), None)
        if declared is None:
            problem(where, f"{path[0][1]!r} is not a declared input")
            return None
        if not declared.default and not declared.choices:
            problem(f"inputs.{declared.name}", "a runner input needs a default or choices")
        for choice in (*declared.choices, *((declared.default,) if declared.default else ())):
            if choice not in RUNNER_POLICIES:
                check(choice, f"inputs.{declared.name}")
        return RunnerRule("input", declared.name)
    if isinstance(raw, str) and raw.strip():
        check(raw.strip(), where)
        return RunnerRule("literal", raw.strip())
    if isinstance(raw, Mapping):
        _known(raw, FIELDS["runner"], f"{where}.", problem)
        relation = [key for key in ("not", "same") if key in raw]
        if len(relation) != 1:
            problem(where, "must be a runner, ${inputs.NAME}, {not: NODE} or {same: NODE}")
            return None
        other = raw[relation[0]]
        if other not in agents or other == node_id:
            problem(f"{where}.{relation[0]}", f"{other!r} is not another agent node")
            return None
        choices = raw.get("choices") or []
        if not isinstance(choices, list) or not all(isinstance(choice, str) for choice in choices):
            problem(f"{where}.choices", "must be a list of runners")
            choices = []
        for index, choice in enumerate(choices):
            check(choice, f"{where}.choices[{index}]")
        pool = choices or list(available or ())
        if relation[0] == "not" and available is not None and len(pool) < 2:
            problem(where, "{not: ...} needs at least two runners to choose between")
        return RunnerRule(relation[0], other, tuple(choices))
    problem(where, "required; a runner, ${inputs.NAME}, {not: NODE} or {same: NODE}")
    return None


def _outputs(raw: object, where: str, problem: Any) -> tuple[OutputSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping) or not raw:
        problem(where, "must be a mapping of output name to {type, enum, required}")
        return ()
    found = []
    for name, value in raw.items():
        path = f"{where}.{name}"
        if not isinstance(name, str) or not _IDENTIFIER.match(name):
            problem(path, "an output name is letters, digits and '_'")
            continue
        value = value or {}
        if not isinstance(value, Mapping):
            problem(path, "must be a mapping")
            continue
        _known(value, FIELDS["output"], f"{path}.", problem)
        enum = value.get("enum") or []
        if not isinstance(enum, list) or not all(isinstance(item, str) for item in enum):
            problem(f"{path}.enum", "must be a list of text")
            enum = []
        kind = FIELD_RULES["output"]["type"].read(value, "type", f"{path}.type", problem)
        if enum and kind != "string":
            problem(f"{path}.enum", "only a string output can have an enum")
        lineage = value.get("lineage", False) in (True, "required")
        if lineage and kind != "findings":
            problem(f"{path}.lineage", "only findings carry lineage")
        required = FIELD_RULES["output"]["required"].read(
            value, "required", f"{path}.required", problem)
        found.append(OutputSpec(
            name, kind, tuple(enum), required, lineage, str(value.get("description") or ""),
        ))
    return tuple(found)


def _facets(raw: object, where: str, problem: Any) -> tuple[Branch, ...]:
    """`- {facet: security, model: elevated}`: one parallel execution per facet."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        problem(where, "must be a list of facets such as {facet: security}")
        return ()
    branches = []
    for index, entry in enumerate(raw):
        path = f"{where}[{index}]"
        if not isinstance(entry, Mapping) or "facet" not in entry:
            problem(path, "must be a mapping with a facet, such as {facet: security}")
            continue
        item = dict(entry)
        facet = item["facet"]
        if isinstance(facet, str):
            if facet not in FACETS:
                problem(f"{path}.facet", f"unknown facet {facet!r}; built in: {', '.join(FACETS)}")
                continue
            item["facet"] = FACETS[facet]
        elif isinstance(facet, Mapping):
            if not all(isinstance(facet.get(key), str) and facet.get(key) for key in ("id", "name", "focus")):
                problem(f"{path}.facet", "a custom facet needs id, name and focus")
                continue
            item["facet"] = {"elevated": bool(facet.get("elevated", False)), **facet}
        else:
            problem(f"{path}.facet", "a built-in facet id or {id, name, focus}")
            continue
        branch_id = str(item["facet"]["id"])
        if not _BRANCH.match(branch_id):
            problem(f"{path}.facet.id", "letters, digits, '_' and '-'")
            continue
        branches.append(Branch(branch_id, item))
    ids = [branch.id for branch in branches]
    if len(set(ids)) != len(ids):
        problem(where, "each facet may appear once")
    return tuple(branches)


def _written_order(ids: Sequence[str], flow: Sequence[FlowRule]) -> tuple[FlowRule, ...]:
    """The edges the written order implies, where `flow` does not say otherwise.

    The first step starts; each step goes on to the next, and the last to end,
    unless `flow` gives that step a way out of its own.
    """
    if not ids:
        return ()
    routed = {source for rule in flow for source in rule.sources}
    found = [] if START in routed else [FlowRule((START,), (ids[0],))]
    for step, following in zip(ids, (*ids[1:], END)):
        if step not in routed:
            found.append(FlowRule((step,), (following,)))
    return tuple(found)


def _flow(raw: object, ids: set[str], problem: Any) -> tuple[FlowRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        problem("flow", "a list of edges such as `a -> b` and routes")
        return ()
    rules: list[FlowRule] = []

    def endpoint(name: str, path: str, *, source: bool) -> bool:
        if name == START and source or name == END and not source or name in ids:
            return True
        problem(path, f"{name!r} is not a step" + ("" if name not in (START, END) else
                f"; {name} can only be a {'source' if name == START else 'target'}"))
        return False

    for index, entry in enumerate(raw):
        path = f"flow[{index}]"
        if isinstance(entry, str):
            match = _ARROW.match(entry)
            if not match:
                problem(path, "an edge is `a -> b`, or `[a, b] -> c` to wait for both")
                continue
            sources, join = _names(match.group(1))
            targets, _ = _names(match.group(2))
            if all(endpoint(s, path, source=True) for s in sources) and all(
                endpoint(t, path, source=False) for t in targets
            ):
                if START in sources and len(sources) > 1:
                    problem(path, "start cannot be joined")
                    continue
                rules.append(FlowRule(tuple(sources), tuple(targets), join=join))
        elif isinstance(entry, Mapping):
            _known(entry, FIELDS["flow"], f"{path}.", problem)
            sources, join = _names(entry.get("from"))
            if not sources or not all(endpoint(s, f"{path}.from", source=True) for s in sources):
                if not sources:
                    problem(f"{path}.from", "required")
                continue
            if ("to" in entry) == ("route" in entry):
                problem(path, "give either to or route")
                continue
            if "to" in entry:
                targets, _ = _names(entry["to"])
                if targets and all(endpoint(t, f"{path}.to", source=False) for t in targets):
                    rules.append(FlowRule(tuple(sources), tuple(targets), join=join))
                continue
            if len(sources) != 1 or sources[0] == START:
                problem(f"{path}.from", "a route starts from one node")
                continue
            routes = _routes(entry["route"], f"{path}.route", endpoint, problem)
            if routes:
                rules.append(FlowRule(tuple(sources), routes=routes))
        else:
            problem(path, "an edge string or {from, to} or {from, route}")
    return tuple(rules)


def _routes(raw: object, where: str, endpoint: Any, problem: Any) -> tuple[Route, ...]:
    if not isinstance(raw, list) or not raw:
        problem(where, "a list of {when, to}, ending with a {to} default")
        return ()
    routes = []
    for index, branch in enumerate(raw):
        path = f"{where}[{index}]"
        if not isinstance(branch, Mapping) or "to" not in branch:
            problem(path, "must be {when, to} or {to}")
            continue
        _known(branch, FIELDS["route"], f"{path}.", problem)
        when = branch.get("when") or ""
        if not isinstance(when, str):
            problem(f"{path}.when", "must be an expression")
            continue
        if when:
            try:
                parse(when)
            except ExpressionError as error:
                problem(f"{path}.when", str(error))
        targets, _ = _names(branch["to"])
        if targets and all(endpoint(t, f"{path}.to", source=False) for t in targets):
            routes.append(Route(when, tuple(targets)))
    if routes and routes[-1].when:
        problem(where, "the last branch must be a default {to: ...} with no when")
    if any(not route.when for route in routes[:-1]):
        problem(where, "only the last branch may omit when")
    return tuple(routes)


def _loop(raw: object, problem: Any) -> LoopDefaults:
    if raw is None:
        return LoopDefaults()
    if not isinstance(raw, Mapping):
        problem("loop", "must be a mapping such as {every: 6h, instruction: ...}")
        return LoopDefaults()
    _known(raw, FIELDS["loop"], "loop.", problem)
    interval = None
    if raw.get("every") is not None:
        try:
            interval = parse_duration(raw["every"])
        except ValueError as error:
            problem("loop.every", str(error))
    instruction = FIELD_RULES["loop"]["instruction"].read(
        {"instruction": raw.get("instruction") or ""}, "instruction", "loop.instruction", problem)
    return LoopDefaults(instruction.strip(), interval)


# --- whole-graph checks -------------------------------------------------------


def _structure(spec: GraphSpec, problem: Any) -> None:
    edges = spec.edges()
    if not spec.entries:
        problem("flow", "no node to start with: add `start -> NODE`")
        return
    forward: dict[str, set[str]] = {}
    for source, target, _ in edges:
        forward.setdefault(source, set()).add(target)
    reached = _reach(START, forward)
    for node in spec.nodes:
        if node.id not in reached:
            problem(node.path, "is never reached from start")
    backward: dict[str, set[str]] = {}
    for source, target, _ in edges:
        backward.setdefault(target, set()).add(source)
    finishing = _reach(END, backward)
    for node in spec.nodes:
        if node.id in reached and node.id not in finishing:
            problem(node.path, "never reaches end")
    plain: dict[str, set[str]] = {}
    for source, target, conditional in edges:
        if not conditional:
            plain.setdefault(source, set()).add(target)
    if cycle := _cycle(plain):
        problem("flow", f"{' -> '.join(cycle)} loops with no route out; put a route in the loop")
    routed = [rule.sources[0] for rule in spec.flow if rule.routes]
    for source in sorted({source for source in routed if routed.count(source) > 1}):
        problem("flow", f"{source} has more than one route; merge them into one")
    for rule in spec.flow:
        if not rule.routes and set(routed) & set(rule.sources):
            problem("flow", f"{', '.join(sorted(set(routed) & set(rule.sources)))} has both a route "
                    "and plain edges out; use only the route")


def _references(spec: GraphSpec, input_names: set[str], problem: Any) -> None:
    """Every name an expression reads must exist, and outputs must be able to have run."""
    forward: dict[str, set[str]] = {}
    for source, target, _ in spec.edges():
        forward.setdefault(source, set()).add(target)
    ids = {node.id for node in spec.nodes}

    def check(expression: Expression, where: str, at: str, *, item: bool) -> None:
        for path in expression.references:
            root = path[0]
            if root == "inputs":
                if len(path) < 2 or path[1] not in input_names and path[1] != INSTRUCTION:
                    problem(where, f"{'.'.join(path)}: not a declared input")
            elif root in ("outputs", "visits"):
                if len(path) < 2 or path[1] not in ids:
                    problem(where, f"{'.'.join(path)}: not a step")
                elif root == "outputs" and path[1] != at and at not in _reach(path[1], forward):
                    problem(where, f"{'.'.join(path)}: {path[1]} cannot have run before {at}")
            elif root == "item" and not item:
                problem(where, "item is only defined in a step with facets")

    for node in spec.nodes:
        where = node.path
        parallel = bool(node.parallel)
        for field_name, text in (("prompt", node.prompt), ("model", node.model)):
            if text:
                parsed = _template(text, f"{where}.{field_name}", problem)
                for expression in parsed.expressions if parsed else ():
                    check(expression, f"{where}.{field_name}", node.id, item=parallel)
        if node.choose:
            check(parse(node.choose), f"{where}.human.choose", node.id, item=False)
    for index, rule in enumerate(spec.flow):
        for route_index, route in enumerate(rule.routes):
            if route.when:
                check(parse(route.when), f"flow[{index}].route[{route_index}].when", rule.sources[0], item=False)


def _reach(start: str, graph: Mapping[str, set[str]]) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        for following in graph.get(stack.pop(), ()):
            if following not in seen:
                seen.add(following)
                stack.append(following)
    return seen


def _cycle(graph: Mapping[str, set[str]]) -> list[str]:
    state: dict[str, int] = {}
    trail: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        trail.append(node)
        for following in sorted(graph.get(node, ())):
            if state.get(following) == 1:
                return [*trail[trail.index(following):], following]
            if following not in state and (found := visit(following)):
                return found
        trail.pop()
        state[node] = 2
        return []

    for node in sorted(graph):
        if node not in state and (found := visit(node)):
            return found
    return []


# --- helpers ------------------------------------------------------------------


def _names(raw: object) -> tuple[list[str], bool]:
    """`a`, `[a, b]` or a YAML list, and whether it was a list (a join)."""
    if isinstance(raw, list):
        return [str(item).strip() for item in raw], len(raw) > 1
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("[") and text.endswith("]"):
            names = [part.strip() for part in text[1:-1].split(",") if part.strip()]
            return names, len(names) > 1
        return ([text] if text else []), False
    return [], False


def _template(text: str, where: str, problem: Any) -> Template | None:
    try:
        return template(text)
    except ExpressionError as error:
        problem(where, str(error))
        return None


def _text(raw: Mapping[str, Any], key: str, where: str, problem: Any) -> str:
    value = raw.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        problem(where, "must be text")
        return ""
    return value.strip()


def _known(raw: Mapping[str, Any], allowed: set[str], prefix: str, problem: Any) -> None:
    for key in raw:
        if key not in allowed:
            problem(f"{prefix}{key}", f"unknown field; expected one of {', '.join(sorted(allowed))}")


__all__ = [
    "API_VERSION",
    "Branch",
    "FlowRule",
    "GraphError",
    "GraphSpec",
    "InputSpec",
    "LoopDefaults",
    "NodeSpec",
    "OutputSpec",
    "Problem",
    "Route",
    "RunnerRule",
    "SECTIONS",
    "STAGES",
    "STAGE_GROUPS",
    "WORKSPACE_NODE",
    "parse_duration",
    "parse_graph",
]

"""The rules of a Graph that span fields, which its JSON Schema cannot say.

Ported from V1's `graph_service/language.py`: step names are unique and not
reserved, `flow` names steps that exist, every step is reachable from `start`
and can reach `end`, every cycle passes through a `route`, a step has at most
one route, and every expression parses and reads only names the graph
declares. `openengine.contracts.resources` runs these after the schema, on
whatever of the document is well-formed enough to read, so both kinds of
problem are reported together.

Nothing here runs a graph or evaluates an expression, and runners and tools
are not looked up: whether they exist is known only when a run starts.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from openengine.contracts.expressions import ExpressionError, holes, parse

START, END, NAMING = "start", "end", "naming"
RESERVED = (START, END, NAMING)
STAGES = ("plan", "implementation", "review")

Report = Callable[[str, str], None]


@dataclass(frozen=True)
class _Step:
    name: str
    path: str
    """Where the step is written, such as `spec.plan[0]`."""
    body: Mapping[str, Any]


@dataclass(frozen=True)
class _Branch:
    when: str | None
    to: str | None
    path: str


@dataclass(frozen=True)
class _Rule:
    path: str
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    routes: tuple[_Branch, ...]
    unknown: bool
    """Whether `to` names a step that does not exist."""


def check(document: Any) -> list[tuple[str, str]]:
    """Every graph-level problem in a Graph, as `(path, message)`, such as `("spec.flow[2].to", "unknown step 'x'")`."""
    spec = document.get("spec") if isinstance(document, Mapping) else None
    if not isinstance(spec, Mapping):
        return []
    found: list[tuple[str, str]] = []

    def problem(path: str, message: str) -> None:
        found.append((path, message))

    written = _written(spec)
    if not written:
        problem("spec", "the graph has no steps; add one to plan, implementation or review")
    steps = _steps(written, problem)
    rules = _flow(spec.get("flow"), steps, problem)
    edges = _edges(list(steps), rules)
    _structure(steps, rules, edges, problem)
    _references(spec, written, steps, rules, edges, problem)
    return list(dict.fromkeys(found))


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _written(spec: Mapping[str, Any]) -> list[_Step]:
    """Every named step in the written order, stage by stage, duplicates and reserved names included."""
    return [
        _Step(body["name"], f"spec.{stage}[{index}]", body)
        for stage in STAGES
        for index, body in enumerate(_list(spec.get(stage)))
        if isinstance(body, Mapping) and isinstance(body.get("name"), str)
    ]


def _steps(written: list[_Step], problem: Report) -> dict[str, _Step]:
    """Each step by name, the first of any duplicates, and none with a reserved name."""
    steps: dict[str, _Step] = {}
    for step in written:
        if step.name in RESERVED:
            problem(f"{step.path}.name", f"'{step.name}' is reserved")
        elif step.name in steps:
            problem(f"{step.path}.name", f"duplicate step '{step.name}'; {steps[step.name].path} has the same name")
        else:
            steps[step.name] = step
    return steps


def _flow(raw: Any, steps: Mapping[str, _Step], problem: Report) -> list[_Rule]:
    def known(name: Any, path: str, *, source: bool) -> bool:
        if not isinstance(name, str):
            return False
        if name == (START if source else END) or name in steps:
            return True
        if name in (START, END):
            problem(path, f"{name} can only be a {'source' if name == START else 'target'}")
        else:
            problem(path, f"unknown step '{name}'")
        return False

    rules = []
    for index, entry in enumerate(_list(raw)):
        if not isinstance(entry, Mapping):
            continue
        path = f"spec.flow[{index}]"
        written = _list(entry.get("from"))
        sources = tuple(name for name in written if known(name, f"{path}.from", source=True))
        if START in sources and len(written) > 1:
            problem(f"{path}.from", "start cannot be joined")
        written_targets = _list(entry.get("to"))
        targets = tuple(name for name in written_targets if known(name, f"{path}.to", source=False))
        routes = tuple(
            _Branch(
                branch.get("when") if isinstance(branch.get("when"), str) else None,
                branch.get("to") if known(branch.get("to"), f"{path}.route[{position}].to", source=False) else None,
                f"{path}.route[{position}]",
            )
            for position, branch in enumerate(_list(entry.get("route")))
            if isinstance(branch, Mapping)
        )
        if routes:
            if len(written) != 1 or START in written:
                problem(f"{path}.from", "a route leaves exactly one step")
            if routes[-1].when is not None:
                problem(f"{routes[-1].path}.when", "the last branch is the default, which has no when")
            for branch in routes[:-1]:
                if branch.when is None:
                    problem(branch.path, "only the last branch can leave out when")
        rules.append(_Rule(path, sources, targets, routes, len(targets) < len(written_targets)))
    return rules


@dataclass(frozen=True)
class _Edges:
    plain: dict[str, set[str]]
    """Unconditional edges, by source."""
    every: dict[str, set[str]]
    """Every edge, routes included, by source."""


def _edges(order: list[str], rules: list[_Rule]) -> _Edges:
    """The written order, where `flow` does not say otherwise, and `flow`.

    The first step starts; each step goes on to the next, and the last to end,
    unless a rule leaves that step.
    """
    plain: dict[str, set[str]] = {}
    every: dict[str, set[str]] = {}
    routed = {source for rule in rules for source in rule.sources}

    def add(source: str, target: str, *, conditional: bool) -> None:
        every.setdefault(source, set()).add(target)
        if not conditional:
            plain.setdefault(source, set()).add(target)

    if order and START not in routed:
        add(START, order[0], conditional=False)
    for step, following in zip(order, (*order[1:], END)):
        if step not in routed:
            add(step, following, conditional=False)
    for rule in rules:
        for source in rule.sources:
            for target in rule.targets:
                add(source, target, conditional=False)
            for branch in rule.routes:
                # An unknown target is already reported; reading it as `end` keeps it from failing every step before it.
                add(source, branch.to or END, conditional=True)
            if rule.unknown:
                add(source, END, conditional=True)
    return _Edges(plain, every)


def _structure(steps: Mapping[str, _Step], rules: list[_Rule], edges: _Edges, problem: Report) -> None:
    reached = _reach(START, edges.every)
    backward: dict[str, set[str]] = {}
    for source, targets in edges.every.items():
        for target in targets:
            backward.setdefault(target, set()).add(source)
    finishing = _reach(END, backward)
    for step in steps.values():
        if step.name not in reached:
            problem(step.path, f"step '{step.name}' is never reached from start")
        elif step.name not in finishing:
            problem(step.path, f"step '{step.name}' never reaches end")
    if cycle := _cycle(edges.plain):
        problem("spec.flow", f"cycle {' -> '.join(cycle)} does not pass through a route")
    routed: set[str] = set()
    for rule in rules:
        if rule.routes and len(rule.sources) == 1:
            if rule.sources[0] in routed:
                problem(rule.path, f"step '{rule.sources[0]}' has more than one route; merge them into one")
            routed.add(rule.sources[0])
    for rule in rules:
        for source in sorted(routed & set(rule.sources)) if rule.targets else ():
            problem(rule.path, f"step '{source}' has both a route and plain edges out; use only the route")


def _references(
    spec: Mapping[str, Any], written: list[_Step], steps: Mapping[str, _Step], rules: list[_Rule], edges: _Edges,
    problem: Report,
) -> None:
    """Every expression parses, and reads only inputs and steps that exist, and outputs that can have run."""
    inputs = {name for entry in _list(spec.get("inputs")) if isinstance(name := _mapping(entry).get("name"), str)}

    def check(source: str, where: str, at: str, *, item: bool) -> None:
        try:
            expression = parse(source)
        except ExpressionError as error:
            problem(where, str(error))
            return
        for path in expression.references:
            root, name = path[0], path[1] if len(path) > 1 else ""
            if root in ("inputs", "outputs", "visits") and not name:
                problem(where, f"{root} needs a name, such as {root}.{'NAME' if root == 'inputs' else 'STEP'}")
            elif root == "inputs" and name not in inputs:
                problem(where, f"undeclared input '{name}'")
            elif root in ("outputs", "visits") and name not in steps:
                problem(where, f"unknown step '{name}' in {root}.{name}")
            elif root == "outputs" and name != at and at not in _reach(name, edges.every):
                problem(where, f"outputs.{name} cannot have run before {at}")
            elif root == "item" and not item:
                problem(where, "item is only defined in a step with facets")

    def text(owner: Mapping[str, Any], key: str, where: str, at: str, *, item: bool = False) -> None:
        if isinstance(owner.get(key), str):
            for hole in holes(owner[key]):
                check(hole, f"{where}.{key}", at, item=item)

    def expression(owner: Mapping[str, Any], key: str, where: str, at: str) -> None:
        if isinstance(owner.get(key), str):
            check(owner[key], f"{where}.{key}", at, item=False)

    # The checkout and naming come before any step, so no step's output exists yet.
    text(_mapping(spec.get("workspace")), "ref", "spec.workspace", START)
    naming = _mapping(spec.get("naming"))
    text(naming, "model", "spec.naming", START)
    text(_mapping(naming.get("runner")), "name", "spec.naming.runner", START)
    # Duplicates and reserved names are already reported; their bodies are checked all the same.
    for step in written:
        agent, human = _mapping(step.body.get("agent")), _mapping(step.body.get("human"))
        where = f"{step.path}.agent"
        faceted = bool(agent.get("facets"))
        runner = _mapping(agent.get("runner"))
        text(runner, "name", f"{where}.runner", step.name, item=faceted)
        for key in ("sameAs", "differentFrom"):
            if isinstance(runner.get(key), str) and runner[key] not in steps:
                problem(f"{where}.runner.{key}", f"unknown step '{runner[key]}'")
        text(agent, "model", where, step.name, item=faceted)
        text(agent, "prompt", where, step.name, item=faceted)
        for index, facet in enumerate(_list(agent.get("facets"))):
            text(_mapping(facet), "model", f"{where}.facets[{index}]", step.name, item=True)
        text(human, "prompt", f"{step.path}.human", step.name)
        expression(human, "choose", f"{step.path}.human", step.name)
    for rule in rules:
        for branch in rule.routes:
            if branch.when is not None and len(rule.sources) == 1:
                check(branch.when, f"{branch.path}.when", rule.sources[0], item=False)


def _reach(start: str, graph: Mapping[str, set[str]]) -> set[str]:
    seen, stack = {start}, [start]
    while stack:
        for following in graph.get(stack.pop(), ()):
            if following not in seen:
                seen.add(following)
                stack.append(following)
    return seen


def _cycle(graph: Mapping[str, set[str]]) -> list[str]:
    """One cycle, as the steps around it ending where it began; empty for none."""
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

# Graph definitions (draft v1)

A graph is either a **Python LangGraph file** or a **YAML file**. Both become
the same thing — a `GraphWorkflow` (an uncompiled LangGraph `StateGraph` built
from the engine's components) — and both run on the existing graph engine
(`engine.graph_runtime_langgraph`) of the backend you choose: same
checkpoints, approvals, steering, resumption and run feed.

```text
graph.py   ──load (workflow loader)──┐
                                     ├─▶ GraphWorkflow ──▶ backend daemon: LangGraphRuntime
graph.yaml ──parse/validate/compile──┘                     (register a version, start runs)
```

- **YAML** is the simple format: agent steps, a checkout, a person, and the
  flow between them, with plain conditions. Starter graphs and most
  user-written graphs are YAML.
- **Python** is the full format: anything LangGraph and the components can
  express — computed prompts, custom routers, `Send` fan-out, custom node
  classes. It is exactly what `workflows/*.py` already is (a module exporting
  `workflow`, built with `graph_workflow`), so
  `engine graph add workflows/implementation_review_graph.py` just works.

There is one registry, one version history and one `exec` for both. A
version records which format it came from and keeps its source verbatim,
which is what `engine graph get` returns.

## Python files

```bash
engine graph add ./my_graph.py
```

The file is uploaded as source and loaded on the backend by the same loader
that reads a daemon's `workflows/` directory (factored out to load one file).
It must export `workflow` — one `GraphWorkflow` or a tuple — exactly as
workflow files do today. The registry, not the file, assigns the graph and
version ids; the file's `id` becomes the default name.

Python runs on the backend with the daemon's privileges, so uploading it is
an operator action: `/api/v1` already admits only operators and the service
token, and a backend can refuse Python entirely with
`[graphs] allow_python = false` in its `engine.toml`. YAML never executes
code and is always accepted.

## YAML files

```yaml
apiVersion: openengine.cc/v1
kind: Graph
name: spec-tdd-review
description: Spec it, test it first, implement, wait for CI, then review.

inputs:                         # creation fields; `engine graph exec` fills instruction
  instruction: {required: true, description: The change to make}

plan:
  workspace:                    # the checkout; optional, first if written
    base_ref: origin/HEAD
  spec:
    agent: claude
    prompt: "Write a short spec for: ${instruction}"
  tdd:
    agent: {same: spec}
    prompt: "Write failing tests for this spec: ${outputs.spec}"

implementation:
  implementer:
    agent: {same: spec}
    prompt: |
      Implement ${outputs.spec} until the tests pass.
      Findings to address, if any: ${json(outputs.reranker.findings)}
  ci_check:
    ci: true

review:
  reviewers:
    agent: {not: implementer}
    facets:                     # one parallel review per facet
      - {facet: security, model: elevated}
      - {facet: bugs, model: default}
    prompt: "Review the change for ${item.facet.name}: ${item.facet.focus}"
    outputs:
      findings: {type: findings, required: true}
  reranker:
    agent: {same: implementer}
    prompt: "Consolidate these reviews: ${json(outputs.reviewers)}"
    outputs:
      findings: {type: findings, required: true, lineage: required}

flow:                           # only what the written order cannot say
  - from: reranker
    route:
      - {when: "size(outputs.reranker.findings) > 0 && visits.implementer < 2", to: implementer}
      - {to: end}
```

**Stages.** Steps are written in three sections, `plan`, `implementation`
and `review`, which run in that order. Each section's steps run in the order
written, and its section is the step's stage in the CLI's display. A step name
is used once across the whole graph.

**Steps** take their kind from their keys. Names are yours:

| keys | becomes | |
| --- | --- | --- |
| `agent:` + `prompt:` | `ACPNode` (+ `TerminalMcpServer` with `tools`/`outputs`) | one langgraph-acp session per execution |
| `human:` | `HumanReviewNode` | waits for a person's decision |
| `ci: true` | `CICheck` | waits for the change's CI |
| none of those | `WorkspaceNode` | the checkout: `base_ref`, `ref: ${inputs.NAME}` |

A checkout must be the first step. If none is written, the graph starts with
one named `workspace`, and `workspace` can only name a checkout. Agent steps
also take `name`, `description`, `tools` (broker tools such as
`git_subcommand`, `open_pull_request`), `outputs` (validated by
`type`/`enum`/`required`; without it the output is the agent's final message),
`model`, and `steering: always-open`. An agent step with `facets:` runs once
per facet, in parallel. A facet is a built-in id or `{id, name, focus}`, and
its other fields (`model`, ...) become `item`. Its output is the list of
every facet's output.

**Flow.** The written order is the default flow: the first step starts, each
step goes on to the next, and the last goes to `end`. `flow` adds what the
order cannot express. `a -> b` is an edge, `[a, b] -> c` waits for both, and
`route` is a conditional edge whose branches are tried in order, the last
unconditioned one being the default. A step given an edge or route in `flow`
leaves the written order at that point, so it goes only where `flow` says
(`start -> x` likewise replaces the first step as the entry). Cycles are
allowed only through a `route`, so a loop always has a way out. Every step
must be reachable and able to reach `end`.

**Expressions** in `${...}` and `when:` are deliberately small: names
(`instruction`, `repository`, `inputs.X`, `outputs.NODE[.FIELD]`,
`visits.NODE`), literals, comparisons, `&& || !`, and `size`/`json`. They are
evaluated by a restricted interpreter — no Python, no attribute access beyond
those names — and checked at registration. Anything needing more is a sign the
graph should be Python.

## Backends

A graph is backend-neutral. `engine graph add|exec --backend NAME` sends it to
that backend's daemon, which validates it against its own runners and
components, builds the `GraphWorkflow`, and registers it on its
`LangGraphRuntime` under an immutable version id. Runners (`claude`, `codex`)
resolve in that backend's configuration; how they are isolated (process
today, smolvm later) is the backend's concern, not the graph's.

A daemon's `workflows/` directory may hold `*.yaml` beside `*.py`, so built-in
and registered graphs are one set of formats.

## Validation

YAML: unknown fields, undeclared inputs, unknown runners or tools, expression
errors, references to outputs of nodes that cannot have run yet, unreachable
nodes, nodes that cannot reach `end`, unconditional cycles — all reported at
once with a path (`review.reviewers.outputs.findings`). A runner taken from
an input (`agent: ${inputs.agent}`) is checked when a run starts instead, so a
graph whose default runner a backend lacks still registers there; a run naming
a runner the backend does not offer is refused with the ones it does.

Python: the file must import and export `workflow`; LangGraph must compile it
against the backend's checkpointer. Its import error or compile error is
returned as the problem.

## What changes from today

The current manifest (agent nodes in a DAG, `{{ }}` placeholders) becomes
this YAML: stage sections in place of `nodes` and `stage:`, the written order
as the default flow, `${ }` placeholders, `flow` with `route` (so loops and
branches), `facets`, `outputs`, `human`, `ci`, and the `apiVersion/kind`
header. The registry gains a `format` and `source` per version. Starter graphs
(`review`, `implement-review`, `spec-implement-review`) are written in it, and
`engine onboard` and interactive `graph exec` land on top.

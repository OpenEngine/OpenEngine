---
title: Graphs
description: A graph is a versioned definition of how a change gets made. Every run goes through one.
---

A graph is a versioned definition of how a change gets made. Every run goes through one.

Each step is a stage: a checkout, an agent writing code, reviewers, a CI check, a person signing off. The flow between steps can branch and loop back, so a review with findings can send the change back to be fixed. Graphs are YAML or Python files you keep in your repository and register with a backend, so changes to your process get reviewed like any other code.

## Register and run a graph

```bash
engine graph add ./graph.yaml
engine graphs list --pretty
engine graph get implement-review --pretty   # or implement-review@2, a graph id or a version id
engine graph run implement-review "Retry webhook deliveries with backoff" --wait
```

A graph's name belongs to a project, and its versions are immutable. Registering a changed definition adds a version; registering an identical one does nothing. A run pins the version it started on, so registering a new version never changes a run already underway.

`graph run` returns the run ID immediately; `--wait` follows it to the end and exits non-zero if it failed. Inspect runs with `engine run get RUN_ID --pretty` and `engine runs --graph NAME --status failed`. Runs appear in the web UI and are approved under the daemon's `[approvals]` policy.

## Built-in graphs

These need no `graph add`. The first `graph run` of one in a project registers it.

| Graph | What it does |
| --- | --- |
| `review` | Reviews your branch from four angles, consolidates the findings, and reports them. It changes nothing. |
| `implement-review` | Implements a change, reviews it from four angles with a different agent, and fixes what survives once. |
| `spec-implement-review` | Writes a short spec, implements it, reviews the result against the spec with a different agent, and fixes what survives once. |
| `adversarial-review` | Checks out `--branch`, has `--agent` attack the change from three angles, then tries to refute every finding and reports what survives. It changes nothing. |

```bash
engine graph run adversarial-review --branch feat/my-feature --agent codex --wait
```

## Write a YAML graph

Steps go in three sections, `plan`, `implementation` and `review`, which run in that order and in the order written within each. A step's keys decide its kind:

| Keys | Step |
| --- | --- |
| `agent` and `prompt` | An agent session on Claude Code, Codex or opencode. |
| `human` | Waits for a person's decision. |
| `ci: true` | Waits for CI on the change. |
| none of these | The checkout. If you write none, one named `workspace` runs first. |

This graph implements a change, has the other model family review it for security and bugs, and sends real findings back for one fix:

```yaml title="graphs/cross-review.yaml"
apiVersion: openengine.cc/v1
kind: Graph
name: cross-review
description: Implement, review with another agent, fix once.

inputs:
  instruction: {description: The change to make}
  runner:
    default: codex
    choices: [claude, codex, least-utilized]

implementation:
  implementer:
    agent: ${inputs.runner}
    prompt: "Implement this change and leave it uncommitted: ${instruction}"

review:
  reviewers:
    agent: {not: implementer}
    facets:
      - {facet: security, model: elevated}
      - {facet: bugs, model: default}
    prompt: "Review the uncommitted change for ${item.facet.name}: ${item.facet.focus}"
    outputs:
      findings: {type: findings, required: true}
  fix:
    agent: {same: implementer}
    prompt: "Fix these findings: ${json(outputs.reviewers)}"

flow:
  - from: reviewers
    route:
      - {when: "size(outputs.reviewers) > 0 && visits.fix < 1", to: fix}
      - {to: end}
  - fix -> reviewers
```

- **Agents.** `agent` names a runner on the backend, takes one from an input, or picks relative to another step: `{same: STEP}` or `{not: STEP}`. Reviewing with a different model family than the one that implemented catches what the first one misses.
- **Facets.** A step with `facets` runs once per facet, in parallel, and its output is the list of every facet's output.
- **Outputs.** Declared outputs are validated by `type`, `enum` and `required`; without them, a step's output is the agent's final message.
- **Flow.** The written order is the default. `flow` adds edges (`a -> b`), joins (`[a, b] -> c`) and conditional `route`s. A cycle must pass through a route, so every loop has a way out.
- **Expressions.** `${...}` and `when:` use a small, checked language: `instruction`, `repository`, `inputs.NAME`, `outputs.STEP`, `visits.STEP`, `item`, comparisons, `&& || !`, and `size`, `json`, `has`, `lower`. They cannot run Python.

Registration reports every problem at once, each with its path: unknown fields, undeclared inputs, unavailable outputs, unreachable steps and unconditional cycles. The [graph specification](cli-specs.md) lists every field and constraint, and `engine graph spec` prints it offline.

## Write a Python graph

When YAML is not enough, such as computed prompts, custom routers or your own node classes, write the graph in LangGraph. The file exports `workflow`, built with `graph_workflow` and the engine's components (`WorkspaceNode`, `ACPNode`, `ReviewNode`, `RerankerNode`, `CICheck`, `HumanReviewNode`):

```bash
engine graph add workflows/implementation_review_graph.py
```

Python runs on the backend with the daemon's privileges, so only operators can register it, and a backend can refuse it with `[graphs] allow_python = false` in `engine.toml`. YAML never executes code. Both formats share one registry, version history, checkpoints, approvals, steering and run feed.

## While it runs

```bash
engine nodes list --run RUN_ID --pretty
engine node steer EXECUTION_ID "Use the existing retry helper instead" --wait
```

Steering sends a running agent an updated instruction for its next turn. It moves from `accepted` to `delivered` to `applied`, or ends `rejected` or `undelivered` when the attempt has no live session. Steering is not a retry and not an approval.

To run something on a schedule, add a [loop](loops.md). Every command is in the [CLI reference](cli-reference.md).

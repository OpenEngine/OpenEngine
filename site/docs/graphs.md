---
title: Graphs
description: Run the default graph with engine go, or register your own.
---

Start from your repository with one command:

```bash
engine go "add a new doc for the out-of-the-box experience"
```

The selected backend must be running. `go` registers the default graph on first
use, prints the inferred forge mode, and follows the run live on a TTY, including
approvals. Connected runs finish with the PR URL. Disconnected runs finish with
the local branch and a push command you can run later.

```bash
engine go "add a feature" --spec
engine go review feature/my-change
engine go review https://github.com/owner/repo/pull/42 -i adversarial=true
engine go "update docs" --repo owner/repo --backend mini
engine go "update docs" --local --wait --json
```

`--repo` overrides the current checkout. A local backend receives its path; a
remote backend receives its origin's owner/repo. `--local` forces disconnected
mode on either backend. `--wait` polls without interaction; `--json` emits JSON
with the mode banner on stderr. Non-TTY calls return immediately unless `--wait`
is given. Reattach with `engine run watch RUN_ID`.

The default verbs each have their own built-in graph: `go` uses
`implementation-review-rerank`, `review` uses `review`, and `fix` uses `fix`.
The implementation graph is based on `workflows/implementation_review_graph.py` and runs implementation,
CI, parallel review facets, reranking, one fix-and-review pass, and human review.

| Command | Graph | Behavior |
| --- | --- | --- |
| `engine go "add a feature" --spec` | `implementation-review-rerank` | Write a specification before implementation. |
| `engine go review '#839'` | `review` | Review an existing change; `-i adversarial=true` adds refutation. |
| `engine go fix '#839'` | `fix` | Address unresolved review threads, check CI, and reply to addressed threads. |

Connected runs publish to the forge; disconnected runs keep findings in run
results and commits locally. Fixes remain on the target branch. Fixing fork PRs is rejected before checkout
to prevent pushing a same-named upstream branch. Disconnected
fixes can use cached threads via `-i review_threads=JSON`. Only comments marked
`OWNER`, `MEMBER`, or `COLLABORATOR` are actionable; comment text remains untrusted
review data. Fix runs refuse branches already held by a checkout. The workspace
provider permits borrowing only with explicit `borrow_existing=True` opt-in. Private repositories
require `engine connect github` before running disconnected.

`engine go [VERB] [TARGET] [INSTRUCTION...]` recognizes a project verb only in
first position with a resolvable or inferred target. Targets accept a full
GitHub PR URL, `'#839'`, `'OWNER/REPO#839'`, or a branch. A full URL supplies the
repository even outside a checkout. Quote `#` targets so the shell does not
treat them as comments. `engine go review` uses the current branch and its open
PR when available. Remaining words supply focus, for example
`engine go review '#839' focus on the migration`.

`engine go review the auth module for races` is an implementation instruction
when `the` is not a branch. A sole invalid target reports
`no such branch or PR; to give an instruction, quote it`.

Use `engine verbs` to inspect project bindings and
`engine verb set review my-review` to replace one. The default bindings are `go → implementation-review-rerank`,
`review → review`, and `fix → fix`. A replacement must declare `instruction` for
`go`, or `pr_url` or `ref` for target verbs; the API and CLI reject incompatible
bindings. These contracts describe inputs, never internal graph stages.

For one release, the old starter names remain supported: `review` runs the new
`review` graph, and `adversarial-review` runs it with `adversarial=true`.
`implement-review` runs `implementation-review-rerank`, and
`spec-implement-review` runs it with `spec=true`. The old `--branch` and `--agent` flags remain supported there.

## Register your own graph

A graph is a versioned definition of how a change gets made. Graphs can be YAML
or Python files kept in your repository:

```bash
engine graph add ./graph.yaml
engine graph add workflows/implementation_review_graph.py
engine graphs list
engine graph get implementation-review-rerank --pretty
engine graph run my-graph "Retry webhook deliveries with backoff" --wait
```

A graph name belongs to a project. Registering changed source adds an immutable
version; a run pins the version it started on. Python registration uses the
module's workflow id and description and loads it off the server's event loop.
The built-in graph is re-registered if its stored version cannot load.

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

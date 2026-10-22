---
slug: continuous-security-review
title: Letting OpenEngine review its own public API security
authors: [sheahawkins]
tags: [openengine, graphs, loops, security]
---

A coding agent has access to things that matter: repositories, tools, credentials and the ability to start work. OpenEngine exposes some of those capabilities through APIs. Every new endpoint is another place where we need to ask whose authority a request carries and where its input can go.

We built a daily loop that asks those questions about OpenEngine itself. One agent investigates the public API surface. A different agent tries to disprove its findings with isolated reproductions. The final step turns confirmed findings into scoped GitHub issues that an engineer can pick up and rectify.

The whole thing is one [graph file](https://github.com/OpenEngine/OpenEngine/blob/main/docs/examples/graphs/security-review.yaml) and one `engine loop add`. The findings land as public issues in [OpenEngine/OpenEngine](https://github.com/OpenEngine/OpenEngine/issues?q=is%3Aissue+%22openengine-security%22), so you can watch what it catches.

<!-- truncate -->

The result we want is a repair queue. An issue should explain the affected interface, the conditions needed to trigger the problem, the demonstrated impact, and what a fix must prove. A recurring report that nobody can act on would leave the important work unfinished.

## Start at the boundary

The review starts with the code behind externally callable interfaces: the graph daemon's `/api/v1`, the web API, login and OAuth callbacks, webhooks, tool handlers, streams and artifact routes. It discovers the actual route registrations in the reviewed checkout and distinguishes running implementations from proposed contracts.

From each entry point, the investigator follows input through authentication, authorization and the operation it reaches. Can this user select another repository? Does this path stay inside the permitted workspace? Can a URL reach an unintended service? Does a graph submission acquire privileges its caller should not have?

The tests run locally with synthetic identities and stubbed dependencies. A reproduction must exercise the real route or public handler, including the guards around it. Calling a vulnerable-looking helper directly can miss the middleware that makes the attack impossible.

This is source-assisted defensive review of the public surface. The loop does not probe our live service, other people's infrastructure or real accounts. The control plane also uses public interfaces: graph registration, scheduling and inspection go through the OpenEngine CLI and its HTTP API. No database writes or private orchestration hooks are needed to install it.

## Three steps, each with a concrete output

The complete, executable [security-review graph](https://github.com/OpenEngine/OpenEngine/blob/main/docs/examples/graphs/security-review.yaml) lives beside the other example graphs. It uses the current `openengine.cc/v1` graph language.

| Step | Job | Output |
| --- | --- | --- |
| `investigate` | Map entry points and trace plausible attacks | Reviewed commit, coverage, test evidence and up to five candidates |
| `validate` | Independently reproduce or reject each candidate | Confirmed findings, rejected or unverified candidates, and reasons |
| `scope_issues` | Deduplicate and scope remediation work | Verified GitHub issue links, deferred drafts and a final report |

The investigator defaults to Claude. The validator declares `agent: {not: investigate}`, so OpenEngine selects a different runner. The issue-scoping step uses the validator's runner. Each step declares typed outputs; subsequent steps receive those outputs explicitly.

```yaml
inputs:
  runner:
    default: claude
    choices: [claude, codex]
  max_new_issues:
    default: "3"
    choices: ["1", "2", "3"]
```

This is an excerpt; use the linked file for registration. The graph includes the prompts, checkout, output declarations and schedule.

Each run reviews an `origin/main` snapshot and records its exact commit. The daily focus rotates through authentication, ownership isolation, injection, OAuth and webhooks, execution privileges, data exposure, and coverage gaps. Adjacent boundaries are fair game when the evidence leads there. The report names both inspected and unreviewed areas.

The validator's job is to challenge the first agent. It checks mount paths, middleware, operator gates, defaults and the attacker's real permissions. A finding advances only after an independent reproduction demonstrates security impact. A failed setup or a plausible static suspicion stays in the run output as unverified.

## End with an issue somebody can fix

The final step files issues in `OpenEngine/OpenEngine`. OpenEngine's current repository tools do not include issue creation, so this step uses the installed `gh` client against GitHub's public API with the host's existing authentication.

Every new issue must contain:

- The affected commit, public interface, severity rationale and attacker prerequisites.
- The violated boundary, root cause and code references pinned to that commit.
- A local reproduction using synthetic data, with setup, exact command or request, and expected versus actual behavior.
- Independent validation commands and their results.
- A proposed fix scope, explicit non-goals, acceptance checkboxes and regression test cases.

The graph asks for a title such as `fix(security): enforce repository ownership on artifact reads`. That is an example of the format, not a claim that this vulnerability exists.

It also checks the repository's security policy before publishing. Secrets, personal data and deployment identifiers do not belong in an issue body. If policy requires private disclosure, the detailed draft stays in the run output and publishing is reported as blocked. The article describes the process; it does not publish unreviewed vulnerability details.

### Give recurring findings a stable identity

A daily loop needs to recognize yesterday's finding. The validator derives a fingerprint from the public surface, root cause and violated boundary. Dates, commit IDs and line numbers are excluded because those change without changing the underlying problem.

The publisher includes that fingerprint in an issue-body marker:

```html
<!-- openengine-security:FINGERPRINT -->
```

Before creating anything, it reads open and closed issues and compares both markers and meaning. An existing open issue gets a comment only when the evidence or proposed scope materially changes. The loop does not replace a person's issue description or automatically close issues. A reproduced regression against a closed issue gets a new linked issue.

The publisher rechecks before creation and looks for the marker after an ambiguous timeout before retrying. This reduces duplicates; it is not an atomic GitHub idempotency guarantee. The marker is a convention enforced by the agent's instructions.

At most three new issues may be created per run, including regressions. That cap is an explicit graph input, constrained to one, two or three, and passed in the deployment command. The agent must count its successful creations and retain additional drafts in the report. This is a prompt-level publishing limit, not a server-enforced GitHub quota. Zero confirmed findings means zero new issues.

## Put the loop on the Mac mini

The Mac mini already runs an OpenEngine backend named `mini`. The CLI can register and schedule work there while the laptop remains the client. Verify the available runners and read the language the installed version supports:

```bash
uv run engine backends list --check
uv run engine graph spec
uv run engine loop spec
```

From an OpenEngine checkout, register the complete graph:

```bash
uv run engine graph add docs/examples/graphs/security-review.yaml \
  --backend mini --project default
```

Registration returns an immutable graph version. On our deployment, the final definition registered as version 2. Pin that version when adding the loop; another installation should use the version its registration returns:

```bash
uv run engine loop add security-review@2 \
  --backend mini --project default \
  --repo OpenEngine/OpenEngine \
  --max-spend 25 \
  --input max_new_issues=3
```

The graph supplies a one-day cadence and its recurring instruction. The first tick is due immediately; `--no-run-now` would delay it by an interval. The daemon schedules subsequent runs, so closing the laptop does not stop the loop. Runs within one loop do not overlap.

The $25 limit counts cumulative reported agent usage for the loop's lifetime. It is not a daily allowance or a complete billing cap: subscription usage can be unpriced, and hosting and CI are excluded. Inspect `spendEnforceable` and usage completeness before relying on that number. This graph opens no pull requests, so a PR-count limit would not bound issue creation.

## Check execution, not just registration

```bash
uv run engine loop get security-review --backend mini --json
uv run engine runs list --graph security-review --backend mini --json
# Use a run ID returned above:
uv run engine run get RUN_ID --backend mini --json
```

The loop record shows its pinned version, cadence, inputs, budget, active run and latest completed output. The final output lists verified issue links, existing matches, deferred drafts, rejected candidates and publishing blockers. A successful registration alone does not prove that a review executed or that GitHub accepted an issue.

During installation, we found two server processes sharing the mini's graph database. One handled registration while another scheduler claimed the first tick without having that graph version loaded in memory. The loop appeared active, but had no recorded run; the daemon log reported that the pinned version was unavailable. We registered the identical pinned source through the second process's public HTTP API, then paused and resumed the existing loop. Its next tick started `run-ff061969507d`. This is why deployment verification includes an actual run, not just the loop's `state` field.

As of October 21, the deployed loop is `loop-c73a2024c893`, pinned to `security-review` v2, with a one-day cadence, `max_new_issues=3` and a $25 lifetime reported-usage limit. The first review is running; no completed security findings or issue-creation results are claimed here. Loading both processes resolves this installation's missing-version problem, but future graph updates need the same care until the deployment uses a single scheduler or synchronizes its loaded registry.

For routine operation, pause scheduling with:

```bash
uv run engine loop pause security-review --backend mini \
  --reason "Review the current repair queue"
```

Pausing does not cancel an active run. Resume with `engine loop resume security-review --backend mini`; if the lifetime spending limit has been reached, supply a higher approved limit with `--max-spend`. Permanent shutdown uses `engine loop stop`, which also cancels the active run.

Graph updates do not silently change this loop. Register a new version, pause the old loop, and add the new version under another loop name. That keeps the review procedure attached to each run auditable.

The operational measure is the repair queue: reproducible issues with a clear scope, enough evidence to evaluate the claim, and tests that can demonstrate the fix. The run's coverage record tells us what the agents actually examined, including what they could not verify.

## Run it on your own repository

Nothing in the mechanics is specific to OpenEngine. Copy the graph, replace the `OpenEngine/OpenEngine` references in its prompts with your repository, and adjust the daily focus list and entry points to your own API surface. Then register it and add a loop with limits, with a `gh` login on the daemon's machine:

```bash
npm i -g @openengine.cc/engine
engine graph add my-security-review.yaml
engine loop add my-security-review --repo OWNER/REPO --max-spend 25 --input max_new_issues=3
```

If you try it, we would like to hear what it finds and what it gets wrong. Open an issue, join the [Slack](https://join.slack.com/t/openenginegroup/shared_invite/zt-49mkaebkz-m86SbPAwn_QNMPqsSgioYQ), or star the [repository](https://github.com/OpenEngine/OpenEngine) to follow the loop's results.

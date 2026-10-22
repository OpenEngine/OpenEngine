---
slug: continuous-security-review
title: Using OpenEngine to continuously discover security vulnerabilities
authors: [sheahawkins]
tags: [openengine, graphs, loops, security]
---

Agent capabilities are outstripping human abilities to keep up with security vulnerabilities and patching. In order to keep up,
agents must be continuously penetration testing all software for vulnerabilities. Using OpenEngine, we set a budget and a 
and instruct agents to continuously penetration test our software in a loop.

One agent investigates the public API surface. A different agent tries to disprove its findings with isolated reproductions. The final step turns confirmed findings into scoped GitHub issues that an engineer can pick up and rectify.

<!-- truncate -->

The result we want is a repair queue. An issue should explain the affected interface, the conditions needed to trigger the problem and the demonstrated impact. A recurring report that nobody can act on would leave the important work unfinished.

## Start at the boundary

For us, review starts with the code behind externally callable interfaces: the graph daemon's `/api/v1`, the web API, login and OAuth callbacks, webhooks, tool handlers, streams and artifact routes. It discovers the actual route registrations in the reviewed checkout and distinguishes running implementations from proposed contracts.

From each entry point, the investigator follows input through authentication, authorization and the operation it reaches. Can this user select another repository? Does this path stay inside the permitted workspace? Can a URL reach an unintended service? Does a graph submission acquire privileges its caller should not have?

The tests run locally with synthetic identities and stubbed dependencies. A reproduction must exercise the real route or public handler, including the guards around it. Calling a vulnerable-looking helper directly can miss the middleware that makes the attack impossible.

This is source-assisted defensive review of the public surface. No database writes or private orchestration hooks are needed to install it.

## Three steps

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
- Tags indicating it as a security ticket.
- A local reproduction using synthetic data, with setup, exact command or request, and expected versus actual behavior.
- Independent validation commands and their results.
- A proposed fix scope, explicit non-goals, acceptance checkboxes and regression test cases.

The graph asks for a title such as `fix(security): enforce repository ownership on artifact reads`. That is an example of the format, not a claim that this vulnerability exists.

It also checks the repository's security policy before publishing. Secrets, personal data and deployment identifiers do not belong in an issue body. If policy requires private disclosure, the detailed draft stays in the run output and publishing is reported as blocked.

## Put the loop on the Mac mini

In our deployment, we have a Mac mini running 24/7. It acts as our loop runner. The Mac mini already runs an OpenEngine backend named `mini`. The CLI can register and schedule work there while the laptop remains the client. Verify the available runners and read the language the installed version supports:

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

Registration returns an immutable graph version. On our deployment, the final definition registered as version 2. Pin that version when adding the loop:

```bash
uv run engine loop add security-review@2 \
  --backend mini --project default \
  --repo OpenEngine/OpenEngine \
  --max-spend 25 \
  --input max_new_issues=3
```

The graph supplies a one-day cadence and its recurring instruction. The first run is due immediately; `--no-run-now` would delay it by an interval. The daemon schedules subsequent runs on the Mac mini, so closing the laptop does not stop the loop.

The $25 limit counts cumulative reported agent usage for the loop's lifetime. It is not a daily allowance or a complete billing cap: subscription usage can be unpriced, and hosting and CI are excluded. Inspect `spendEnforceable` and usage completeness before relying on that number. This graph opens no pull requests, so a PR-count limit would not bound issue creation.

## Check execution, not just registration

```bash
% uv run engine loops list
NAME                        GRAPH               STATE   EVERY  NEXT                       ACTIVE RUN        PRS  SPEND         PAUSED BECAUSE
security-review             security-review v2  paused  1d     -                          run-ff061969507d  0/∞  $0.01/$25.00  Redeploying validator on Claude Opus 4.8
security-review-claude-4-8  security-review v3  active  1d     2026-10-22T22:49:18+00:00  run-6f9faf091579  0/∞  $3.95/$25.00  -

% uv run engine run watch run-6f9faf091579
security-review v3 · run-c94a844b2267 · running · 0:04

  ⠴ Plan ─── – Implement ─── ○ Review

  Graph  ✓ workspace → ⠴ investigate → ○ validate → ○ scope_issues
  Now    investigate · claude · attempt 1
  ✓ workspace · Preparing a working copy at origin/main.

  ┃ HEAD matches origin/main. Mapping entry points next.
  ┃ ⚙ Terminal

```

The loop record shows its pinned version, cadence, inputs, budget, active run and latest completed output. The final output lists verified issue links, existing matches, deferred drafts, rejected candidates and publishing blockers.

It works too. Our agent produces security issues daily and we're experimenting with allowing it to automatically produce fixes.

![Open GitHub issues carrying the security label, including findings filed by the security-review loop](/img/blog/security-review-issues.png)

The security-labeled repair queue on October 22, including issues filed by the loop.


```bash
uv run engine loop pause security-review --backend mini \
  --reason "Review the current repair queue"
```

Pausing does not cancel an active run. Resume with `engine loop resume security-review --backend mini`; if the spending limit has been reached, supply a higher approved limit with `--max-spend`. Permanent shutdown uses `engine loop stop`, which also cancels the active run.

Graph updates do not silently change this loop. Register a new version, pause the old loop, and add the new version under another loop name. That keeps the review procedure attached to each run auditable.

The operational measure is the repair queue: reproducible issues with a clear scope, enough evidence to evaluate the claim, and tests that can demonstrate the fix. The run's coverage record tells us what the agents actually examined, including what they could not verify.

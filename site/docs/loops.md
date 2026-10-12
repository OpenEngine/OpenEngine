---
title: Loops
description: A loop runs a graph on a cadence, within limits on pull requests and spend.
---

A loop runs a graph on a cadence, within limits on pull requests and spend.

Use one for recurring work: fix a flaky test every six hours, triage new issues each morning, keep dependencies current. A loop pins one graph version, so registering a new version never changes what an existing loop runs.

## Add a loop

```bash
engine loop add fix-flaky-test --every 6h --max-prs 5 --max-spend 20
engine loops list --pretty
engine loop get fix-flaky-test --pretty
```

The instruction and cadence come from the graph's `loop` section, and `--instruction` and `--every` override them:

```yaml
loop:
  every: 6h
  instruction: Find and fix one flaky test.
```

A loop needs both an instruction and a cadence; neither is invented. Cadences are durations such as `90s`, `30m`, `6h` or `1d`, at least 60 seconds apart. The first run starts now unless you pass `--no-run-now`. `--name` defaults to the graph's name, and `--input NAME=VALUE` and `--repo` are passed to every run.

## Scheduling

- **Runs never overlap.** A tick that comes due while a run is active waits for it, and every tick missed meanwhile collapses into that one.
- **Ticks happen once.** Each tick is recorded before its run starts, so a duplicate tick starts nothing.
- **State survives restarts.** Limits are cumulative over the loop's lifetime and recomputed from durable events.

## Limits

| Option | Limit |
| --- | --- |
| `--max-prs N` | Pull requests the loop's runs *open*, not updates to existing ones. Reaching it pauses the loop; the run that opened the last one may finish. |
| `--max-spend USD` | Agent usage reported by each node's session, or tokens priced at list rates. The active run reserves the remaining budget and is cancelled as soon as spend reaches the cap. |

Spend excludes CI, hosting and subscription-billed usage, which agents report without a price. When any usage is unpriced, `engine loop get` shows `spendEnforceable: false`, and the cap is not presented as enforceable.

A loop also pauses when a run fails because a runner needs signing in. Fix it with `engine agent signin AGENT`.

## Pause and resume

```bash
engine loop pause fix-flaky-test --reason "release freeze"
engine loop resume fix-flaky-test --max-spend 40
```

Pausing stops new runs; it does not cancel the active one. Resuming is refused while a limit is still reached, so pass a higher `--max-prs` or `--max-spend`.

See runs a loop started with `engine runs --loop fix-flaky-test`. The [loop specification](cli-specs.md#engine-loop-spec) has the full rules, and `engine loop spec` prints it offline. Every command is in the [CLI reference](cli-reference.md).

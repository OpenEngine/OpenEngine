---
name: manage-graphs
description: Write, register, run, schedule and debug OpenEngine graphs with the `engine` CLI. Use when asked to add or change a graph, run one, set up or adjust a loop, check on a run or loop, steer a node, or work out why a graph will not register or a run failed.
---

# Managing OpenEngine graphs

Graphs, runs and loops live on a daemon (a *backend*). The `engine` CLI talks
to it over HTTP; it never runs a graph itself. Inside this repository, run the
CLI as `uv run engine ...`.

## Read the spec first

The specifications are generated from the parser and are the only reliable
reference. Read them before writing or editing a graph, rather than copying
an older example (the YAML in `cli/README.md` predates the current language):

```bash
uv run engine graph spec    # YAML and Python graph language
uv run engine loop spec     # cadence, limits, lifecycle
```

Both work offline. Unknown fields are rejected, so a field the spec does not
list will not work.

## Pick the backend

```bash
uv run engine backends list --check --pretty
```

`local` (`http://127.0.0.1:4364`) always exists. Use `--backend NAME` for a
one-off, and `--project NAME` when the graph lives outside the backend's
default project. If `local` is down, ask the user before starting a daemon.

## Write a graph

Start from `apiVersion: openengine.cc/v1`, `kind: Graph`, a `name` matching
`^[a-z0-9][a-z0-9._-]{0,62}$`, and steps under `plan`, `implementation` and
`review`. Guidelines:

- Give each agent step `outputs` with types when a later step or a person
  reads its result; refer to them as `${outputs.STEP.field}` and pass
  structured values with `${json(...)}`.
- Say in the prompt whether the agent may change files. A report-only graph
  should say "do not edit anything" in every prompt.
- Use `agent: {not: STEP}` for a reviewer or challenger, so it is a different
  runner from the one whose work it checks.
- Declare every input the prompts use. `instruction` is built in.
- Add a `loop` section (`every`, `instruction`) when the graph is meant to
  recur, so `engine loop add` needs no flags.
- Put example graphs in `docs/examples/graphs/`; a test parses each one.

## Register and inspect

```bash
uv run engine graph add path/to/graph.yaml
uv run engine graphs list --pretty
uv run engine graph get NAME --pretty        # also NAME@VERSION, g-..., gv-...
```

Versions are immutable. Registering a changed file adds a version;
registering an identical one changes nothing. Registration reports every
problem at once with its path; fix them all and register again.

## Run once

```bash
uv run engine graph run NAME "instruction" --repo OWNER/REPO -i KEY=VALUE --wait
uv run engine run get RUN_ID --pretty
uv run engine runs --graph NAME --status failed --pretty
```

`graph run` prints the run id straight away; `--wait` polls and exits 1 if the
run fails. A run pins the version that was latest when it started. Resubmit
with the same `--idempotency-key` to avoid starting a second run.

`run get` shows status, each node's result, usage, pull requests and the
failure. To follow or correct a node while it works:

```bash
uv run engine nodes list --run RUN_ID --pretty
uv run engine node get EXECUTION_ID --pretty
uv run engine node steer EXECUTION_ID "use the existing retry helper" --wait
```

Steering is a message into the turn the agent is in, not a retry or an
approval. A run stopped on an approval or a human step is answered in the web
UI; `run get` lists what it is waiting for.

## Run on a schedule

```bash
uv run engine loop add NAME --max-spend 20 --max-prs 5   # --every/--instruction override the graph's loop
uv run engine loops list --pretty
uv run engine loop get NAME --pretty
uv run engine loop pause NAME --reason "release freeze"
uv run engine loop resume NAME --max-spend 40
```

- Always set `--max-spend`, and `--max-prs` for a graph that opens pull
  requests, unless the user says otherwise. Limits are cumulative over the
  loop's lifetime.
- The first run starts at once; add `--no-run-now` to wait one interval.
- A loop pins the graph version that was latest when it was added.
  Registering a new version does not move it. To pick up a new version, pause
  the old loop and add a new one under another `--name`.
- `resume` is refused while a limit is still reached; pass a higher limit.
- A loop pauses itself when a run fails because an agent needs to sign in.

## When something fails

| Symptom | Check |
| --- | --- |
| Registration refused | The listed paths against `engine graph spec`: unknown fields, undeclared inputs, outputs not yet available, unreachable steps. |
| Run failed: agent not signed in | `uv run engine agent signin AGENT` on the backend's host; `engine agents --pretty` lists what exists. |
| Run failed otherwise | `failure` in `engine run get RUN_ID --pretty`, then the node's own result with `engine node get`. |
| Loop paused | `pauseReason` and spend in `engine loop get NAME --pretty`. |
| Run or graph version not found | The version may no longer load (for example an older `apiVersion`). Register the graph again under the current `apiVersion` and start new runs or loops from it. |

## Do not

- Do not edit the generated spec in `cli/client/src/engine/cli/specs.py`;
  change the parser sources and run `python3 scripts/generate_cli_docs.py`.
- Do not start, pause or resume a loop, or run a graph that opens pull
  requests, unless the user asked for it.

# openengine

OpenEngine is your SDLC engine.
Changes -> Pool of Reviewers -> Reranking -> Impact Radius Analysis -> System Diagram -> Safe change 

## Getting started

Requires [uv](https://docs.astral.sh/uv/), Python 3.11+, and Node.js 20.19+.

```bash
uv sync            # install all 16 workspace packages, editable
npm --prefix apps/web install
npm --prefix apps/web run build
uv run pytest      # run the suite, including the boundary checks
```

All three entrypoints run today and report their wiring:

```bash
uv run engine-web
```

## Configuration

Each entrypoint accepts one provider-neutral TOML configuration file:

```bash
uv run engine-web --config ./engine.toml
uv run engine-worker --config ./engine.toml
uv run engine-control-server --config ./engine.toml
```

`--config` takes precedence over the `ENGINE_CONFIG` environment variable. If
neither is set, Engine reads `./engine.toml` when it exists, otherwise it uses
built-in defaults. Configurations are not merged.

The first supported settings describe approval intent in Engine vocabulary:

```toml
[approvals]
auto_approve = false
allow = ["read"]

[approvals.bash]
allow = [
  "uv run pytest **",
  "git status **",
]
ask = ["git push **"]
deny = ["sudo **"]
```

Capabilities are `read`, `edit`, `bash`, `web`, and `mcp`. Configuration is
strict: unknown keys, unknown capabilities, duplicate entries, and incorrectly
typed values stop startup with an error instead of silently weakening a policy.

This first configuration slice only loads and validates the policy. Startup
output says `runner translation not enabled` because translating these settings
to Codex and Claude Code is intentionally a follow-up change; until that lands,
their existing permission defaults remain in effect.

## What is it.

We are building OpenEngine, a system for automating the SDLC and SOP. The key differentiator of OpenEngine is that it is a system for configuring token flow rates and planning according to a timeline.

OpenEngine is fundamentally this: A planning agent which projects the timeline and relative issue + milestone sizes based on the user's stated goals. Then, it automates the distribution and production of the code required to reach those milestones according to the token flow rates set by the engine operator. 

The key concepts are:
- A "Project". An end-to-end product that the operator is working on. Timelines and milestones are associated with this.
- A "Workflow". Workflow runs belong to a Project. The orchestrator is able to kick off workflows which bake in the operators SDLC+SOP.
- A "Conversation". Workflows are comprised of these individual agent interactions. Some Conversations may be implementation. Some may be review. 

Fundamentally your project foreman schedules work, and dispatches work according to your budgets. You can use your subscription budgets, because OpenEngine uses claude and codex CLI under the hood. 

## Shape of the system

```
                   Workflow DSL
                  zero-dep Python
                       │
                       ▼
               ┌──────────────┐
Event + State →│    Engine    │→ Commands
               └──────────────┘
                       │
                       ▼
                    Runtime
                       │
             ┌─────────┼─────────┐
             ▼         ▼         ▼
          Temporal   Agents    Git/Buzz
```

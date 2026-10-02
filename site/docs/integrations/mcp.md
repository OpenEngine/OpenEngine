---
title: MCP
---

Connect Claude Code, or another MCP client, to OpenEngine and let your agent
start WorkOrders, follow their progress and steer them from the conversation you
are already in. Work runs in OpenEngine against the repository and workflow the
host configured; existing review and approval rules still apply.

## What you need

Ask whoever runs your OpenEngine host for:

- **The MCP URL**, for example `https://YOUR-MINI.YOUR-TAILNET.ts.net/mcp`. On
  the host machine itself, `http://127.0.0.1:8765/mcp` also works.
- **An access token** (the host's `OE_MCP_TOKEN`), unless the host set up
  OAuth sign-in, in which case you sign in through your client instead.

Treat the token like a password. Anyone holding it can create, inspect, reset
and steer work in the configured repository. Keep it in a secret manager, never
in a prompt or a committed file, and ask the host to rotate it if it leaks.

Hosting the gateway yourself? Follow the
[remote MCP guide](https://github.com/OpenEngine/OpenEngine/blob/main/docs/remote-mcp.md)
first, then come back here.

## Connect Claude Code

Load the token into your shell, then register the server in your user
configuration so the secret stays out of the repository:

```sh
read -rs OE_MCP_TOKEN && export OE_MCP_TOKEN   # paste the token from your host
claude mcp add --transport http --scope user oe https://YOUR-GATEWAY/mcp \
  --header "Authorization: Bearer ${OE_MCP_TOKEN}"
```

Your shell expands `${OE_MCP_TOKEN}` before Claude Code sees it, so the literal
token is saved in plain text in `~/.claude.json` and later changes to the
variable have no effect. Keep that file private (`chmod 600 ~/.claude.json`),
don't sync or share it, and have the token rotated if it is ever exposed. The
token is also briefly visible to other local users in the process list while
the command runs.

If the host uses OAuth, leave off `--header`. Then run `/mcp` inside Claude
Code: `oe` should show as connected (choose it to sign in when using OAuth)
with seven tools: `create_workorder`, `workorder_status`, `node_status`,
`steer_workorder`, `node_steer`, `create_loop` and `loop_status`. See
[Claude Code's MCP documentation](https://code.claude.com/docs/en/mcp) for other
scopes and options.

## Connect Claude on the web or desktop

Add OpenEngine as a custom connector with the MCP URL. Custom connectors sign in
with OAuth rather than a pasted token, so this requires a host with OAuth
enabled.

## Connect another CLI or client

Any client that supports remote MCP over Streamable HTTP works. Point it at the
MCP URL and send `Authorization: Bearer <token>` on every request. For example,
in the Codex CLI's `~/.codex/config.toml`:

```toml
[mcp_servers.oe]
url = "https://YOUR-GATEWAY/mcp"
bearer_token_env_var = "OE_MCP_TOKEN"
```

If your client restricts which tools the model may call, allow the ones you
intend to use.

## Use it

Ask in plain language; your agent picks the tool:

- *"Create an OpenEngine work order to fix the failing login test and add a
  regression test."* Work starts immediately and you get back a `run_id`. Review
  the tool call before approving it: it starts real work.
- *"When that finishes, start another one to update the changelog."* The second
  WorkOrder is created now and waits until the first succeeds.
- *"What's the status of run-123? Show me the last 20 messages from the
  implementation node."* Status includes the current nodes, recent transcript
  and the pull request URL once there is one.
- *"Tell the running task to include a test for empty input."* The instruction
  reaches the active task without losing its progress.
- *"Go back to the implementation node and rework the fix to preserve the
  existing API."* This stops the run and restarts from before that node with
  your instruction queued. It can repeat work; earlier transcript stays visible.

- *"Create a loop that triages new issues every 30 minutes."* The loop prompts
  an agent on that interval, within the host's loop limits, and you get back
  its `loop_id`.
- *"How is loop-1 doing?"* Loop status shows when it runs next, what it spent
  today and the WorkOrders it created.

### Tool reference

| Tool | Arguments | What it does |
| --- | --- | --- |
| `create_workorder` | `prompt`, optional `depends_on_run_id` | Starts a WorkOrder and returns its `run_id`. With a dependency, it waits for that run to succeed. |
| `workorder_status` | `run_id` | Returns `current_nodes`, `topology`, recent `transcript` per node, `active_executions` (each with an `execution_id`) and `pr_url`. |
| `node_status` | `run_id`, `nodename`, optional `last_n` (1–1000, default 10) | Returns a node's messages, oldest first. Use a `nodeId` from `topology.nodes`. |
| `steer_workorder` | `run_id`, `instruction`, optional `nodename` or `execution_id` | Sends an instruction to an active task. Omit both selectors when only one task is running; use `execution_id` for parallel tasks at the same node. |
| `node_steer` | `run_id`, `nodename`, `instruction` | Resets to the latest checkpoint before a previously reached node and resumes with the instruction queued. |
| `create_loop` | `name`, `prompt`, optional `every_minutes` (default 60), `max_workorders`, `max_daily_spend`, `active_hours_start`, `active_hours_end` | Creates a loop that prompts an agent on that interval to create and steer WorkOrders. Omitted limits use the host's loop settings. |
| `loop_status` | `loop_id` | Returns the loop's schedule and limits, `running`, `next_run_at`, `deferred_until`, `spent_today` and the `workorders` it created. |

Prompts and instructions must be non-blank and at most 100,000 characters.
Status and steering tools only accept runs and loops from the host's configured repository.

## Troubleshooting

- **Nothing is retried automatically.** If a create, steer or reset call times
  out, check the WorkOrder or loop list, `workorder_status` or `loop_status`
  before asking again, or you may start duplicate work.
- **401 when connecting:** the token is missing, wrong or rotated. Check that
  `OE_MCP_TOKEN` was set when you ran `claude mcp add`; after a rotation,
  remove the server with `claude mcp remove --scope user oe` and add it again.
- **403 or 421:** the URL doesn't match the host's configured public address.
  Ask the host for the exact URL.
- **Error mentioning OE HTTP 400:** the host's workflow is unknown or needs
  inputs; ask the host to check its configuration.
- **Status, steering or loop tools fail with an upstream 401:** the host has
  GitHub login enabled. Creating WorkOrders still works, but OpenEngine's
  service token doesn't yet authorize the other routes; use the OpenEngine UI
  instead.

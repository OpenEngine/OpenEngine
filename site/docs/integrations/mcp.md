---
title: MCP
---

Connect an MCP client to create WorkOrders, inspect progress and transcripts,
and send instructions to running tasks. The host chooses the repository and
workflow; existing review and approval rules still apply.

## Connect a client

The remote gateway exposes Streamable HTTP at `https://YOUR-GATEWAY/mcp`.
Configure your client with that URL and an `Authorization: Bearer <token>`
header using the host's `OE_MCP_TOKEN`. Keep the token in private client
configuration: anyone holding it can create, inspect, reset and steer work in
the configured repository. Hosts can alternatively configure an external OAuth
provider. Full hosting and authentication setup:
[remote MCP guide](https://github.com/OpenEngine/OpenEngine/blob/main/docs/remote-mcp.md).

Once connected, tool discovery should list `create_workorder`, `workorder_status`,
`node_status`, `steer_workorder` and `node_steer`. If your client restricts allowed
tools, enable the ones you intend to use.

## Create a WorkOrder

Call `create_workorder` with a description of the change. This example shows MCP
`tools/call` parameters:

```json
{"name": "create_workorder", "arguments": {"prompt": "Fix the failing login test and add a regression test."}}
```

The response contains a `run_id`; work starts before the call returns. To chain
work, pass a previous run's ID as `depends_on_run_id`. The new WorkOrder returns
its ID immediately and waits for that prerequisite to succeed. Time-based
scheduling is not exposed through MCP.

Each creation call starts new work. If a request times out, check the WorkOrder
list before retrying. Steering and reset calls also are not automatically
retried; check status after an uncertain response before repeating them.

## Inspect and steer a WorkOrder

After connecting a client, use the `run_id` returned by `create_workorder` to
follow the work. The examples below show MCP `tools/call` parameters; replace
the sample run, node, and execution IDs with values returned by your gateway.

1. Read the run's status and available nodes:

   ```json
   {"name": "workorder_status", "arguments": {"run_id": "run-123"}}
   ```

   `current_nodes` contains active node IDs, or the next nodes when execution
   is idle. `transcript` groups recent messages by node. For parallel tasks,
   inspect `active_executions` to see each execution's separate messages and
   `execution_id`. `pr_url` is null until a pull request URL is available.

2. Read more context for a node using its `nodeId` from `topology.nodes`:

   ```json
   {"name": "node_status", "arguments": {"run_id": "run-123", "nodename": "implementation", "last_n": 20}}
   ```

   The response's `messages` are ordered oldest first and can include earlier
   attempts at that node. `last_n` defaults to 10 and accepts 1–1000.

3. Send a correction to an active task without resetting its progress:

   ```json
   {"name": "steer_workorder", "arguments": {"run_id": "run-123", "execution_id": "exec-2", "instruction": "Include a regression test for an empty input."}}
   ```

   Pass at most one of `execution_id` or `nodename`. Omitting both works when
   there is exactly one active execution. Use `execution_id` to distinguish
   parallel tasks at the same node.

4. To revisit a previously reached node, reset and supply its instruction in
   one call:

   ```json
   {"name": "node_steer", "arguments": {"run_id": "run-123", "nodename": "implementation", "instruction": "Rework the fix to preserve the existing API."}}
   ```

   This stops current execution and resumes from the latest checkpoint before
   that node, with the instruction already queued. It can repeat work; earlier
   transcript entries remain visible. Check `workorder_status` again afterward.

Prompts and steering instructions must be non-blank and at most 100,000
characters. Status and steering tools reject runs outside the gateway's
configured repository. If your client limits allowed tools, enable the status
and steering tools you intend to use as well as `create_workorder`.

**Current GitHub-login limitation:** OE's service token authorizes only
`POST /api/runs`. When OE enforces GitHub browser login, creation works with
`OE_MCP_ENGINE_TOKEN`, but the status and steering tools receive an upstream
401 because they need additional API routes. The gateway does not forward a
browser session; setting its service token does not enable those routes.

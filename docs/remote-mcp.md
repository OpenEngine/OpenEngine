# Create work orders through remote MCP

`engine-mcp-server` exposes one Streamable HTTP tool at `/mcp`:
`create_workorder(prompt: string) -> {"run_id": "..."}`. It calls OE's
`POST /api/runs`, which starts the graph workflow before returning. The call
returns when execution starts, not when agents finish. Existing workflow review
and approval rules still apply. There are no scheduling parameters or tools.
Each call creates a new work order; after a timeout, check OE before retrying.

The gateway is a separate loopback process. Only its port goes through the
funnel; it does not expose OE's UI, settings, or other APIs. Repository and
workflow are chosen by the host, not the caller. Any holder of the bearer token
can start work in that repository. Keep it out of prompts, URLs, and source
control. Rotate it by changing the private env file and restarting the gateway.

## Host on the Mac mini

1. Install and run OE as described in the [README](../README.md). Verify that
   the configured workflow can start from the UI. OE must remain running at
   `http://127.0.0.1:8000` (or set `OE_MCP_ENGINE_URL` to another loopback origin).
   Run `uv sync --locked --all-packages` from this checkout.
2. Install Tailscale on the mini, sign in, and enable Funnel for the node in your
   tailnet policy. Follow the [Funnel prerequisites](https://tailscale.com/docs/features/tailscale-funnel).
   Reserve a funnel listener for this gateway; do not point it at OE's web port.
3. Copy [oe-mcp.env.example](examples/oe-mcp.env.example) to
   `~/.config/openengine/mcp.env` (create that directory first), then run
   `chmod 600 ~/.config/openengine/mcp.env`. Replace the token using
   `openssl rand -hex 32`, repository with an absolute checkout path on the mini,
   workflow with an installed workflow ID, and public URL with the mini's exact
   HTTPS Funnel origin. Include the port if using a non-default HTTPS port.
4. Start the gateway from the checkout:

   ```sh
   .venv/bin/engine-mcp-server --env-file "$HOME/.config/openengine/mcp.env"
   ```

5. In another terminal, publish the gateway:

   ```sh
   tailscale funnel --bg http://127.0.0.1:8765
   tailscale funnel status
   ```

   Use the HTTPS origin printed by Funnel as `OE_MCP_PUBLIC_URL` and restart
   the gateway if it changed. The client URL is that origin plus `/mcp`.
   [Funnel command reference](https://tailscale.com/docs/reference/tailscale-cli/funnel).
6. For automatic startup at login, edit every `/Users/YOU` and checkout path in
   [com.openengine.mcp.plist](examples/com.openengine.mcp.plist), copy it to
   `~/Library/LaunchAgents/`, stop the foreground gateway, then run:

   ```sh
   launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.openengine.mcp.plist"
   launchctl kickstart -k "gui/$(id -u)/com.openengine.mcp"
   ```

   This is a user LaunchAgent, so the user must be logged in. Keep the mini awake
   and ensure OE and Tailscale also start after reboot. Logs are in
   `~/Library/Logs/oe-mcp*.log`. To unload it, use `launchctl bootout` with the
   same domain and plist path. To stop public access, use `tailscale funnel off`
   for this listener.

## Connect Claude Code

Set `OE_MCP_TOKEN` in the client's environment to the same secret, then add this
server in your private user configuration (do not commit the secret):

```sh
claude mcp add --transport http --scope user oe https://YOUR-MINI.YOUR-TAILNET.ts.net/mcp \
  --header "Authorization: Bearer ${OE_MCP_TOKEN}"
```

Use `/mcp` to check connectivity, then ask Claude to create a work order through
OE. Review the tool call before allowing it: it starts real work immediately.
See [Claude Code's HTTP MCP documentation](https://code.claude.com/docs/en/mcp).

## Connect GPT through the OpenAI Responses API

Configure an MCP tool in your Responses API request as follows; supply the token
from your application's secret store, not from model input:

```python
import os
from openai import OpenAI

client = OpenAI()
tool = {
    "type": "mcp",
    "server_label": "oe",
    "server_url": os.environ["OE_MCP_PUBLIC_URL"].rstrip("/") + "/mcp",
    "authorization": os.environ["OE_MCP_TOKEN"],
    "allowed_tools": ["create_workorder"],
    "require_approval": "always",
}
response = client.responses.create(
    model=os.environ["OPENAI_MODEL"],  # a model supporting remote MCP tools
    input="Use OE to create a work order to fix the failing login test.",
    tools=[tool],
)
print(response.output)
```

Present the returned `mcp_approval_request` to the operator. After approval,
submit `{"type": "mcp_approval_response", "approval_request_id": request_id,
"approve": True}` as input with `previous_response_id=response.id` and the same
`tools=[tool]`. Send the authorization value on every request. The
[OpenAI remote MCP guide](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
describes the approval continuation and authorization fields.

This deployment uses a provisioned bearer secret; it does not implement OAuth
discovery or a login flow. The supported connections above are GPT via the API
and Claude Code with an explicit header. Browser connector flows requiring OAuth
need an OAuth-capable gateway; do not disable authentication to accommodate them.

## Verify and troubleshoot

An unauthenticated `curl -i https://YOUR-MINI.YOUR-TAILNET.ts.net/mcp` must return
401. With the bearer header, an MCP client should initialize and list exactly
`create_workorder`. A successful call returns a run ID which appears immediately
in OE's work-order list. The MCP origin itself does not serve that list.

401 means the token is missing or incorrect; 403/421 means the configured public
origin or Host does not match. Tool errors with OE HTTP 400 usually indicate an
unknown workflow or missing required workflow inputs; use a workflow whose
inputs have defaults. Connection errors mean OE is unavailable or could not
confirm creation. Inspect OE before retrying because a lost response may follow
a successful start. The gateway never retries creation automatically.

# Create work orders through remote MCP

`engine-mcp-server` exposes one Streamable HTTP tool at `/mcp`:
`create_workorder(prompt: string, depends_on_run_id?: string) -> {"run_id": "..."}`.
It calls OE's `POST /api/runs`. Without a dependency, the graph workflow starts
before returning. To chain work orders, pass a previous call's `run_id` as
`depends_on_run_id`; the new work order waits until that prerequisite succeeds.
The call returns the new run ID even while it is waiting. Existing workflow
review and approval rules still apply. Time-based scheduling is not exposed.
Each call creates a new work order; after a timeout, check OE before retrying.

The gateway is a separate loopback process that exposes only its MCP tool.
Funnel visibility applies to the entire HTTPS port, not individual paths: sharing
an origin with OE's interface also publishes its UI, settings, and other APIs,
even if the interface was previously tailnet-only. The gateway's bearer token
does not protect those routes. See [Tailscale's port visibility rules](https://tailscale.com/docs/features/tailscale-serve#limitations).
Repository and workflow are chosen by the host, not the caller. Any holder of the bearer token
can start work in that repository. Keep it out of prompts, URLs, and source
control. Rotate it by changing the private env file and restarting the gateway.

## Host on the Mac mini

1. Install and run OE as described in the [README](../README.md). Verify that
   the configured workflow can start from the UI. Keep OE running. `engine-dev`
   does not guarantee the API port: it uses 8000 when free, otherwise an arbitrary
   free port. For a loopback integration, set `OE_MCP_ENGINE_URL=http://localhost:5173`
   to use the Vite dev server's fixed port; it proxies `/api` to the actual API
   port. GitHub and Slack webhooks use this same indirection. The example env
   file and gateway settings default to `http://127.0.0.1:4364`, the production
   `engine-web` endpoint. When using `engine-dev`, override `OE_MCP_ENGINE_URL`
   to match its actual endpoint.
   Run `uv sync --locked --all-packages` from this checkout.
2. Install Tailscale on the mini, sign in, and enable Funnel for the node in your
   tailnet policy. Follow the [Funnel prerequisites](https://tailscale.com/docs/features/tailscale-funnel).
   Point the gateway's Funnel route at its loopback port, not OE's web port.
   Do not let the gateway take over the origin serving OE's interface: if both
   may be public, keep `/` routed to the interface and mount the gateway at `/mcp`.
   If the interface must remain private, use a separate HTTPS listener for the
   gateway as described in step 5.
3. Copy [oe-mcp.env.example](examples/oe-mcp.env.example) to
   `~/.config/openengine/mcp.env` (create that directory first), then run
   `chmod 600 ~/.config/openengine/mcp.env`. Replace the token using
   `openssl rand -hex 32`, repository with an absolute checkout path on the mini,
   workflow with an installed workflow ID, and public URL with the mini's exact
   HTTPS Funnel origin. Include the port if using a non-default HTTPS port.
   Set `OE_MCP_ENGINE_URL` as described in step 1. If OE has
   [GitHub login](github-login.md) enabled, also set `OE_MCP_ENGINE_TOKEN` to
   the value of OE's `ENGINE_SERVICE_TOKEN` (see
   [Service token for the MCP gateway](github-login.md#service-token-for-the-mcp-gateway)).
   It must differ from `OE_MCP_TOKEN`: clients authenticate to the gateway with
   one, the gateway authenticates to OE with the other, and a client's
   credential is never forwarded. When OE reports that login is required and
   this token is unset, the gateway refuses to start.
4. Start the gateway from the checkout:

   ```sh
   .venv/bin/engine-mcp-server --env-file "$HOME/.config/openengine/mcp.env"
   ```

5. In another terminal, publish the gateway. **This shared-origin setup also
   makes any existing `/` interface handler public**, including one previously
   limited to the tailnet:

   ```sh
   tailscale funnel --bg --set-path /mcp http://127.0.0.1:8765/mcp
   tailscale funnel status
   ```

   Use the HTTPS origin printed by Funnel as `OE_MCP_PUBLIC_URL` and restart
   the gateway if it changed. The client URL is that origin plus `/mcp`.
   Publishing the gateway at `/` would shadow OE's interface when `public_url`
   in `engine.toml` uses the same origin, so browsers would receive
   `{"error":"Unauthorized"}` instead of the UI.

   To keep the interface private, leave it on a tailnet-only Serve listener
   (for example, HTTPS port 443) and publish only the gateway on a separate,
   unused HTTPS port instead of running the shared-origin command above:

   ```sh
   tailscale funnel --bg --https=8443 --set-path /mcp http://127.0.0.1:8765/mcp
   tailscale funnel status
   ```

   Keep OE's web routes off that listener. Set
   `OE_MCP_PUBLIC_URL=https://YOUR-MINI.YOUR-TAILNET.ts.net:8443`, restart the
   gateway, and use `https://YOUR-MINI.YOUR-TAILNET.ts.net:8443/mcp` in clients
   and public health checks below. Leave OE's `public_url` on its private origin.
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

The connections above use the provisioned bearer secret. OAuth-capable clients
can instead use the optional external-provider configuration below.

## Verify and troubleshoot

The loopback health check needs no secret:

```sh
curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/mcp
```

A 401 proves the gateway is listening and enforcing its bearer token. A 200
means authentication is not being applied.

An unauthenticated `curl -i https://YOUR-MINI.YOUR-TAILNET.ts.net/mcp` must return
401. With the bearer header, an MCP client should initialize and list exactly
`create_workorder`. A successful call returns a run ID which appears immediately
in OE's work-order list. The gateway itself does not serve that list.

A tool error with OE HTTP 401 means OE requires GitHub login and rejected the
gateway's service token: check that `OE_MCP_ENGINE_TOKEN` matches OE's
`ENGINE_SERVICE_TOKEN`. 401 from the gateway itself means the token is missing or incorrect; 403/421 means the configured public
origin or Host does not match. Tool errors with OE HTTP 400 usually indicate an
unknown workflow or missing required workflow inputs; use a workflow whose
inputs have defaults. Connection errors mean OE is unavailable or could not
confirm creation. Inspect OE before retrying because a lost response may follow
a successful start. The gateway never retries creation automatically.

## Optional OAuth resource server

The gateway supports the [MCP authorization specification, revision 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
as a resource server only. An external OAuth 2.1/OIDC provider handles login,
consent, client registration, and token issuance; the gateway does not expose
`/authorize`, `/token`, or `/register`.

Leave all OIDC settings unset to keep the existing static bearer-token
deployment unchanged. Configuring `OE_MCP_OIDC_AUDIENCE`, `OE_MCP_ALLOWED_EMAILS`,
or `OE_MCP_OIDC_REQUIRED_SCOPES` without `OE_MCP_OIDC_ISSUER` fails startup,
even if the supplied environment variable is empty. Setting it selects OIDC authentication instead; static
secrets are not accepted in that mode. Configure these values in the same private
`~/.config/openengine/mcp.env` file and restart:

- `OE_MCP_OIDC_ISSUER=https://YOUR-IDENTITY-PROVIDER.example`: the provider's exact
  issuer. The gateway discovers its OIDC metadata and JWKS over HTTPS.
- `OE_MCP_ALLOWED_EMAILS=you@example.com`: required, non-empty comma-separated
  allowlist, compared case-insensitively. Missing or empty configuration prevents
  startup. Access tokens must contain an allowed `email` and `email_verified`
  must be present and the boolean `true`. Missing, false, or non-boolean values
  receive 401; the server log identifies the subject and explains that
  `email_verified` must be present and boolean true. Configure the provider to
  include verified email claims in access tokens.
- `OE_MCP_OIDC_AUDIENCE`: defaults to `OE_MCP_PUBLIC_URL` plus `/mcp`. An explicit
  value must equal that resource URL. Configure the provider to issue JWT access
  tokens with this exact audience using the RFC 8707 `resource` parameter;
  an ID token or a token for another API is not suitable.
- `OE_MCP_OIDC_REQUIRED_SCOPES=work:create`: optional space-separated scopes,
  all of which must be granted in the access token's `scope` claim.

The provider must issue asymmetrically signed JWT access tokens with a `kid`,
`iss`, `sub`, `aud`, and `exp`. RSA, RSA-PSS, and ECDSA SHA-2 algorithms are
supported. Signatures, issuer, expiry, and any `nbf` are verified. Signing keys
are cached for five minutes and refreshed on an unknown key ID. Provider lookup
or verification failures deny access.

**OAuth by itself does not restrict who may authorize:** any user the identity
provider will log in gets a valid token. The email allowlist, and an equivalent
restriction at the provider, are the actual access control. Configure both before
exposing the destructive `create_workorder` tool.

Tailscale Funnel needs its own handler for discovery when routing by path:

```sh
tailscale funnel --bg --set-path /.well-known/oauth-protected-resource/mcp http://127.0.0.1:8765/.well-known/oauth-protected-resource/mcp
```

If using the separate HTTPS 8443 listener described above, add `--https=8443`
to this command too. Without the discovery handler, `/.well-known/...` goes to
the web interface and can return a 200 HTML page rather than JSON.

Verify without credentials:

```sh
curl -i https://YOUR-MINI.YOUR-TAILNET.ts.net/.well-known/oauth-protected-resource/mcp
curl -i https://YOUR-MINI.YOUR-TAILNET.ts.net/mcp
```

Discovery must return JSON naming the resource URL and authorization server.
The MCP endpoint must return 401 with a `WWW-Authenticate: Bearer` challenge
containing `resource_metadata` and, when configured, `scope`. A valid token
missing required scopes receives 403. Configure OAuth clients with the same
public `/mcp` URL; follow the external provider's client-registration setup.

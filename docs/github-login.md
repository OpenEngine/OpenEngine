# GitHub browser login

Register a separate GitHub OAuth App for login and configure its callback as
`https://your-engine-host/api/auth/github/callback`. Set these top-level values
in `engine.toml`:

```toml
github_login_client_id = "your-login-client-id"
github_login_redirect_uri = "https://your-engine-host/api/auth/github/callback"
```

`ENGINE_GITHUB_LOGIN_CLIENT_ID` and `ENGINE_GITHUB_LOGIN_REDIRECT_URI`
environment variables override those defaults. HTTP callbacks are accepted only
for loopback development hosts.

Store `ENGINE_GITHUB_LOGIN_CLIENT_SECRET=your-secret` in a server-local `.env`
beside the loaded `engine.toml` (or in the working directory if no config file
is loaded). This file is gitignored; restrict its permissions to the service
owner (`chmod 600 .env`) and edit it via SSH. The app reads it directly, with
dotenv interpolation disabled, without sourcing it into a shell. An explicit
process environment secret takes precedence. Secrets are not accepted in TOML.
The secret file is reread at each token exchange, so updating it takes effect
without a restart; changing the client ID or callback URL requires a restart.
All three values are required when enabling login.

Visit `/login` and select **Sign in with GitHub** to start the browser
authorization flow. It requests only `read:user`, uses OAuth state and PKCE,
and issues a signed, HttpOnly session cookie at the callback before redirecting
to `/`. The user token is not returned or stored in the server's repository
credential store. This follows
[GitHub's web OAuth flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).

When login is configured, the frontend requires a verified session before
mounting the application. Middleware returns 401 for unauthenticated requests
to protected `/api/` and `/graph/api/` routes; the four GitHub login endpoints and `/api/health` remain public. Slack events bypass browser
session checks and retain Slack signature verification. The frontend rechecks
session status every 30 seconds and unmounts the app if the session is invalid.

## Repository access

A GitHub account alone does not open the app: WorkOrder links are posted to
GitHub issues, which anyone can read. At the callback, the server asks GitHub
whether the signed-in account has write access (write, maintain, or admin) to
the `[github] repository` named in `engine.toml`, counting team and
organization grants. The check uses the server's own `gh` login, not the
user's token. An account without write access is sent to
`/login?error=forbidden` and receives no session. When the check fails or
times out, login is refused with `/login?error=unverified`. Configuring GitHub
login without a `[github] repository` is a configuration error: the server
does not start.
Access is checked at sign-in only, so revoking it takes effect when the
session expires (24 hours) or the server restarts.

## Service token for the MCP gateway

The [remote MCP gateway](remote-mcp.md) creates work orders server to server
and cannot hold a browser session. Set `ENGINE_SERVICE_TOKEN` in the same
server-local `.env` (or the process environment, which takes precedence) and
give the gateway the same value as `OE_MCP_ENGINE_TOKEN`:

```sh
openssl rand -hex 32
```

The middleware accepts `Authorization: Bearer <token>` in place of a session
only for `POST /api/runs`; every other protected route still requires login.
The token must have at least 32 non-whitespace characters; a shorter value
fails startup. Like the client secret, it is never read from TOML and is reread
on each request, so rotating it in `.env` needs no restart (update the
gateway's value and restart the gateway). Leaving it unset admits no service
requests.

Login state and the PKCE verifier live in a signed, HttpOnly browser cookie that expires after ten minutes;
abandoned logins reserve no server slots. Replay protection relies on GitHub
consuming authorization codes once and binding them to the PKCE verifier.
The cookie signing key lives in one process, so a restart requires a new
login and multiple workers require sticky routing or a shared signing key.
Without login configuration the app remains accessible and the status endpoint
reports `loginRequired: false`; starting the OAuth flow returns 503.

## Agent GitHub identity

Agent GitHub API actions in the web composition use only the host's `gh auth`
login: the account shown by `gh auth status` for the OS user that runs the web
process. That account is the PR/comment author. Both GitHub choices in Settings
(**GH CLI** and **GitHub OAuth**) route agent actions through `gh`; GitLab
routing is unchanged. Neither the browser login, a Settings device-flow token,
nor `GITHUB_TOKEN` is used for agent actions. OpenEngine removes
`GITHUB_TOKEN` and `GITHUB_ENTERPRISE_TOKEN` from the environment it passes to
`gh`, so an engine setting cannot override the CLI login. `GH_TOKEN` remains
`gh`'s own setting and is honored. The worker composition still uses its
configured `GITHUB_TOKEN`.

Git commits still use Git's author/committer configuration and configured
agent attribution; pushes still use the host's Git credential helper or SSH
credentials (`gh auth setup-git` makes Git use the same login).

Browser login requests `read:user` and only creates a session cookie. The
separate Settings device flow stores repository connection credentials in the
OS keychain. With browser login enabled, its token and client-ID keys include
the verified session's stable GitHub user ID (for example,
`github-token:user:123`). Pending device flows are also scoped to that ID.
Logging out or renaming an account does not transfer its connection to another
user. Authenticated users never inherit the legacy `github-token` entry: they
must reconnect. Local mode without browser login retains the legacy entry.
These UI connection credentials do not authorize agent GitHub API actions.

The web `--check` wiring report shows whether `gh` is authenticated and as
which account. Successful PR creation logs the returned URL, GitHub's actual
author login, and transport at INFO level. Enable INFO logging to retain this
audit evidence.

For graph runs served by the web app, `compose_app` passes its composed
`source_control` to `build_graph_runtime`; terminal MCP resolves that runtime
capability for `open_pull_request`. Worker-dispatched work uses the worker
composition's independent capability. A historical PR cannot be attributed to
a particular process or token from the code alone: correlate its URL and run
with process logs. No historical run ID, PR URL, or credential audit was
available for this change; the old web keychain and CLI paths could both use a
personal identity, whereas the worker used only its configured token.

# OE MCP OAuth issuer

OE can serve interactive OAuth for remote MCP clients using the existing GitHub
browser login and repository write-access gate. It supports public clients with
CIMD or Dynamic Client Registration, consent, PKCE S256 authorization codes, and
rotating refresh tokens. The gateway's acceptance of OE-issued JWTs is a separate
follow-up: this change does not switch gateway authentication.

## Configuration and public routing

Configure the web application in `engine.toml`:

```toml
public_url = "https://oe.example.com"
# Optional; defaults to public_url + "/mcp". This exact value is the JWT aud.
mcp_resource_url = "https://mcp.example.com/mcp"
github_login_client_id = "YOUR_GITHUB_OAUTH_APP_CLIENT_ID"
github_login_redirect_uri = "https://oe.example.com/api/auth/github/callback"

[github]
repository = "owner/repository"
```

Set `ENGINE_GITHUB_LOGIN_CLIENT_SECRET` using OE's existing login secret setup.
The issuer is enabled when GitHub login and `public_url` are configured.
`public_url` must be an HTTPS origin without a path, query, or fragment; the
issuer is `<public_url>/api/oauth`. The MCP resource must be an absolute HTTPS
URL without a fragment. Startup validates both. Changing the resource invalidates
existing grants; clients must authorize again. Never derive the issuer from an
incoming Host header.

Forward these routes to the OE web backend:

| Route | Method | Purpose |
| --- | --- | --- |
| `/.well-known/oauth-authorization-server/api/oauth` | GET | RFC 8414 discovery for the path-based issuer |
| `/api/oauth/metadata` | GET | Same metadata, within the API prefix |
| `/api/oauth/jwks` | GET | Public signing keys |
| `/api/oauth/register` | POST | RFC 7591 public client registration |
| `/api/oauth/authorize` | GET, POST | Browser login and explicit consent |
| `/api/oauth/token` | POST | Code exchange and refresh |

The well-known route is deliberately **outside `/api`**, as required for an
issuer with a path. Vite proxies `/.well-known/oauth-authorization-server` as
well as `/api`; production reverse proxies must forward both prefixes without
rewriting them. The standard web backend serves the well-known route before
its static client. Also make `/login` and `/api/auth/github/*` reachable for
browser login. TLS must terminate at a trusted proxy or the server. Remote
clients need access to discovery, registration, JWKS, and token endpoints;
users' browsers need access to the login and consent pages.

## Authorization behavior

Authorization requires an OE browser session, an exact registered redirect URI,
`response_type=code`, `code_challenge_method=S256`, and `resource` equal to the
configured MCP resource. The supported scope is `mcp` (also the default).
Responses carry `iss` and echo `state`. Anonymous browsers return through
`/login?return_to=...` using OE's existing safe return-path handling. Consent
shows the self-asserted client name, full client ID (including the CIMD host),
and redirect URI, with session-bound CSRF protection.

Token requests use `application/x-www-form-urlencoded` and `client_id`, without
a client secret. Code exchange also requires `code`, `redirect_uri`, and
`code_verifier`. Refresh uses `grant_type=refresh_token` and `refresh_token`.
The token endpoint accepts an omitted `resource` as the already-bound resource;
an explicit different resource is rejected. Codes expire after 60 seconds and
are single-use. JWTs use ES256, include a `kid`, and expire after 15 minutes;
claims include issuer, GitHub numeric user ID (`sub`), login, audience, client
ID, scope, and issue/expiry timestamps.

The same repository authorization hook used by GitHub login runs again before
consent and before issuing a code, and on every refresh grant. Denial or errors
fail closed. Refresh tokens rotate on every use. Reusing a consumed token
revokes the entire family, including its latest replacement. Families expire
30 days after the original authorization, without sliding expiration. Clients
must serialize refresh operations and replace the stored token after success.
A permission-check failure revokes the family and requires new authorization.
Existing access tokens remain valid until expiry.

Client documents must use HTTPS, contain their exact `client_id`, and provide
`redirect_uris`. OE permits HTTPS redirect URIs and HTTP loopback redirects,
matched exactly. Metadata retrieval has a 16 KiB limit and a five-second total
timeout, rejects all redirects, and connects only to a resolved public address
with certificate verification for the original hostname. Private, loopback,
link-local, and other non-global addresses are rejected, including mixed DNS
answers. Client names are self-asserted, not verified publisher identities.

## Storage and administration

The SQLite state migration adds registered clients, hashed authorization codes,
and hashed refresh tokens. The state store applies the Alembic revision at
startup; operators may also run
`engine-migrate sqlite:////absolute/path/to/state.db`. Private signing material
is stored beside the state file in `<database>.oauth-keys.json` (mode 600). Back up this file securely with
the database. Keys persist across app restarts. Rotation retains old public keys
in JWKS for 15 minutes (the access-token lifetime), then excludes them automatically.
Repeated rotation does not extend old deadlines. Legacy key rings receive a fixed
15-minute overlap on first startup after upgrade. Rotation replaces the private
signing key; running processes reload it.

Administrator commands (run under the web app's OS account):

```sh
uv run --package engine-web python -m engine.apps.web.mcp_oauth_storage rotate-key /path/to/state.db
uv run --package engine-web python -m engine.apps.web.mcp_oauth_storage revoke-all /path/to/state.db
uv run --package engine-web python -m engine.apps.web.mcp_oauth_storage retire-key /path/to/state.db --kid=COMPROMISED_KEY_ID
```

Revocation marks every refresh family revoked and removes outstanding codes.
It does not invalidate already-issued access tokens, which expire after 15
minutes. Retain consumed refresh rows for reuse detection; do not delete them
while their families can still be used.

For a compromised signing key, use `retire-key` with its JWKS `kid`. This removes
its public key immediately; if it is the active key, OE also generates a replacement.
Verifiers must refresh cached JWKS to observe removal; purge their caches during
incident response. Use `revoke-all` as well to invalidate outstanding grants.

Dynamic registration is capped at 1,000 stored clients, enforced atomically across
workers. Registrations expire after 30 days; expired rows are deleted on the next
registration request, and SQLite reuses the freed space. Existing registrations
receive 30 days from migration. At capacity, `/register` returns HTTP 503 with
`temporarily_unavailable`. Clients must register again for new authorizations after
expiry; already-issued grants retain their own expiry. CIMD needs no registration
row and is unaffected by the cap. Request metadata remains limited to 16 KiB.

OAuth database transactions run entirely in worker threads. Read-only lookups use
deferred transactions so they do not acquire SQLite's write lock.

# OE MCP OAuth issuer

The web app's OAuth foundation lives in `mcp_oauth.py`, with ES256 signing keys,
RFC 8414 discovery, public Dynamic Client Registration, and Client ID Metadata
Documents (CIMD). The next stacked change wires these into the web app with
browser consent and authorization-code / refresh-token grants. The gateway
continues to use its existing authentication until a separate gateway change.

Client documents must use HTTPS, contain their exact `client_id`, and provide
`redirect_uris`. OE permits HTTPS redirect URIs and HTTP loopback redirects,
matched exactly. Metadata retrieval has a 16 KiB limit and a five-second total
timeout, rejects all redirects, and connects only to a resolved public address
with certificate verification for the original hostname. Private, loopback,
link-local, and other non-global addresses are rejected, including mixed DNS
answers. Client names are self-asserted, not verified publisher identities.

The SQLite state migration adds registered clients, hashed authorization codes,
and hashed refresh tokens. Private signing material is stored beside the state
file in `<database>.oauth-keys.json` (mode 600). Back up this file securely with
the database. Keys persist across app restarts. Rotation retains old public keys
in JWKS and replaces the private signing key; running processes reload it.

Administrator commands (run under the web app's OS account):

```sh
uv run --package engine-web python -m engine.apps.web.mcp_oauth_storage rotate-key /path/to/state.db
uv run --package engine-web python -m engine.apps.web.mcp_oauth_storage revoke-all /path/to/state.db
```

Revocation marks every refresh family revoked and removes outstanding codes.
It does not invalidate already-issued access tokens, which expire after 15
minutes. Retain consumed refresh rows for reuse detection; do not delete them
while their families can still be used.

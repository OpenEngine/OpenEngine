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

Visit `/api/auth/github/login` to start the browser authorization flow. It
requests only `read:user`, uses OAuth state and PKCE, and returns the verified
GitHub numeric ID and login as JSON at the callback. The user token is not
returned or stored in the server's repository credential store. This follows
[GitHub's web OAuth flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).

This is the identity-verification portion of SSO (#300). Session issuance and
route protection (#301), and repository permission checks (#302), are separate
work; this feature alone does not protect app access. Login state and the PKCE verifier
live in a signed, HttpOnly browser cookie that expires after ten minutes;
abandoned logins reserve no server slots. Replay protection relies on GitHub
consuming authorization codes once and binding them to the PKCE verifier.
The cookie signing key lives in one process, so a restart requires a new
login and multiple workers require sticky routing or a shared signing key.
Without login configuration the login endpoints return 503.


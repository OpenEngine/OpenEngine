# GitHub App identity

Configure a GitHub App to make server GitHub actions appear as `<app-slug>[bot]`.
The WorkOrder requester remains credited through the existing `Co-authored-by`
commit trailer, including snapshots. Browser sign-in does not choose the server
identity. Without app configuration, web and worker use the host's `gh auth`
login; incomplete app configuration fails instead of borrowing that login.

## Create and install the app

Create a GitHub App under your organization or account's Developer settings.
Give it these repository permissions:

| Permission | Access |
| --- | --- |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write |
| Actions | Read and write |
| Checks | Read |
| Metadata | Read |

Install it on every repository Engine will work on. In the app settings,
generate and download a private key. Store it outside the repository, readable
only by the server account. Record the App ID.

Set the webhook URL to `<public_url>/api/github/events`, set a webhook secret,
and subscribe to **Issues**, Issue comment, Pull request review comment, and
Pull request events. Apps cannot be issue assignees: add the `openengine` label
to an open issue to request work. The person applying it must have repository
write access, just as on the assignment path.

## Server configuration

Put these values in the server-local `.env` beside `engine.toml` (or the working
directory when no TOML is loaded):

```dotenv
ENGINE_GITHUB_APP_ID=123456
ENGINE_GITHUB_APP_PRIVATE_KEY_PATH=/absolute/server/path/openengine.pem
ENGINE_GITHUB_WEBHOOK_SECRET=your-webhook-secret
```

Restrict `.env` and the private key to the service owner (`chmod 600`). Never put
app credentials in TOML or commit them. Environment variables take precedence;
`.env` interpolation is disabled. Relative key paths resolve beside `.env`.
Secrets and key contents are reread when used, so key rotation takes effect
without restarting. Restart after initially enabling app mode.

```toml
[github]
repository = "owner/repository"
trigger_label = "openengine"
```

`engine-web --check` reports app configuration or the fallback CLI identity.
The server discovers the slug through `GET /app` and the bot ID through
`GET /users/<slug>[bot]`. Installation tokens are cached until one minute before
expiry, and API requests refresh and retry once after a 401.

Each Engine worktree gets the bot commit identity, an HTTPS URL rewrite, and a
credential helper in its own `--worktree` config. No bearer token is stored in
Git config or its environment. The helper reads server secrets and supplies a
fresh installation token over Git's credential pipe. The provider enables Git's
`extensions.worktreeConfig` capability, as it already does for the co-author
hook; identity, helper and URL rewrite settings never change the repository's
shared or global configuration. Reattaching a worktree reapplies app settings.

See [GitHub webhooks](github-webhooks.md) for delivery authorization and retries,
and GitHub's [installation authentication documentation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation).

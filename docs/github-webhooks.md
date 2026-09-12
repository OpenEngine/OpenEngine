# GitHub comment webhooks

Engine reads comments from GitHub over a signed webhook. The route only exists
once something is wired to answer a comment, so configure the webhook after that
is in place: an endpoint that accepted deliveries it could never act on would
collect failures until GitHub disabled the hook.

Point a GitHub app or a repository webhook at `<public_url>/api/github/events`, subscribe it to the
`issue_comment` and `pull_request_review_comment` events, and give it a secret.

## Naming the repository

Name the repository whose deliveries this deployment answers in `engine.toml`:

```toml
[github]
repository = "owner/name"
```

The slug format is validated at startup. Comments from other repositories are
acknowledged and ignored, even when signed with the same secret. Repository
names are compared without regard to case. Without a configured repository,
actionable comment deliveries receive 503 until setup is complete.

## Storing the secret

The webhook's shared secret is not written in `engine.toml`, which is
committed. Store it as `ENGINE_GITHUB_WEBHOOK_SECRET=your-secret` in a
server-local `.env` beside the loaded `engine.toml` (or in the working
directory when no config file is loaded) — the same file the
[GitHub login](github-login.md) secret uses. It is gitignored; restrict its
permissions to the service owner (`chmod 600 .env`) and edit it over SSH. The
app reads it directly, with dotenv interpolation disabled, without sourcing it
into a shell. A process environment variable of the same name takes precedence,
and the file is reread per delivery, so rotating the secret in GitHub's webhook
settings and on disk takes effect without a restart. Secrets are not accepted
in TOML.

`engine-web --check` reports both halves — the repository and whether a secret
is readable — so a half-finished setup is visible before the first delivery
arrives.

## The account Engine posts as

```bash
GITHUB_BOT_LOGIN=... uv run engine-web
```

`GITHUB_BOT_LOGIN` is the GitHub account Engine posts as, whose own comments are
never answered. Set it whenever Engine authenticates with a personal access
token belonging to a machine user: such an account is an ordinary user and
usually a collaborator, so without this it would answer itself in a loop. A
GitHub app is recognised by its user type and needs no setting.

## What the route does with a delivery

The route verifies the `X-Hub-Signature-256` GitHub sends and refuses anything
it did not sign. A signature only proves GitHub sent the delivery, so authorship
is checked separately: only comments from a repository's owners, members, and
collaborators are acted on, and a bot's own comments are ignored so Engine does
not answer itself.

The route is reachable without a session, so a delivery larger than 2 MB is
refused with 413 before its body is buffered or its signature checked. GitHub
caps its own payloads at 25 MB and a comment event is far smaller than either
figure.

A verified comment is queued and acknowledged immediately, because GitHub gives
a webhook ten seconds before it considers the delivery failed. Each comment is
handled once no matter how often GitHub redelivers it.

The route answers 503 while no secret is set or while the queue is full. In each case the comment is not lost: the
delivery stays visible as failed in the webhook's delivery log and can be
redelivered. If handling a comment fails after the delivery was acknowledged,
the comment is forgotten rather than remembered, so redelivering it from the
delivery log picks the work back up. Events Engine does not read, and the `ping`
GitHub sends when the webhook is saved, are always acknowledged, so a webhook
subscribed to more than Engine needs does not accumulate failed deliveries.

## Steering a work order, or starting one

A comment on a pull request reaches at most one work order, and which one is
the host's decision rather than the answering agent's. Engine looks up the run
working on the pull request — recorded when that run took it on, not inferred
from the conversation — and asks the graph engine what it is doing now:

- running, or waiting on a person: the request is steered into that run, and
  the reply names it.
- finished, failed, no longer registered, or never recorded at all — a pull
  request opened by hand has no such run: a work order is started for the
  repository the comment arrived from, on the workflow named by
  `work_orders.workflow`, and the reply says a work order was started.

A work order started this way claims the pull request as it starts, so later
comments steer it rather than starting another: one work order per pull
request, however many comments arrive. The claim is what settles it when two
comments arrive together and both start — one of them keeps the pull request,
and the run that did not is cancelled rather than left working a branch no
later comment can reach. A work order started here reports nothing back to
chat: the pull request is where the conversation is, and Engine answers it
there.

The agent reading the comment decides only whether it is asking for a change at
all; a comment that asks for nothing reaches no work order. Comments on issues
are not answered.

## Watching what arrives

Every comment the route queues is remembered for the web UI, so a delivery can
be followed without reading GitHub's delivery log or this process's stdout.
Comments are always read beside the work they steered: a WorkOrder's page shows
the comments left on its own pull request, and there is no page or route that
lists every comment this process has seen. The strip under the WorkOrder's
heading carries a `GitHub comments` count linking down to the panel, so the
comments are findable without scrolling for them.

A comment's row says when it was received, when the concierge picked it up, the
WorkOrder its feedback reached -- and whether that WorkOrder was started for
this comment or was already in flight -- and the fixed announcement posted back
to the pull request, with a link to the comment itself at one end and to the
WorkOrder at the other. A comment Engine decided not to act on says so and why
-- "not a pull request", or an author without write access -- which is the
distinction a delivery log cannot draw. Beside them, how many of *these*
comments are still moving -- read off the rows themselves, not off the one
ingress queue or the one concierge, whose depth and busy flag describe
whichever comment is in flight and rarely one of this WorkOrder's.

A comment reaches a WorkOrder's page by the WorkOrder its feedback was
forwarded to, falling back to the pull request that WorkOrder opened. That
fallback is what puts an ignored comment -- which forwarded nothing to be named
by -- in front of the right reader. A comment on a pull request no WorkOrder
opened has no page, and is not shown. The pull request is asked for once, from
the WorkOrder, rather than asking who owns each remembered comment's pull
request and discarding every answer that named somebody else.

No row names its WorkOrder or links to it: every row on the page belongs to the
WorkOrder the page is about, so it says whether the comment reached it, and
whether it started it or steered it, instead.

The record is in memory and bounded, so a restart forgets it. That is
deliberate: it is a window on what is happening, and GitHub's delivery log and
the pull request are the durable record.

It is served from `GET /api/runs/{run_id}/github-comments`.

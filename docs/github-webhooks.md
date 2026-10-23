# GitHub webhooks

Engine reads comments, issue assignments, merges, and CI results from GitHub
over a signed webhook. Comments and assignments request work; merges accept it;
CI results wake a run waiting on CI. The route only exists once something is wired to act on a delivery, so
configure the webhook after that is in place: an endpoint that accepted
deliveries it could never act on would collect failures until GitHub disabled
the hook.

Point a GitHub app or a repository webhook at `<public_url>/api/github/events`, subscribe it to the
`issue_comment`, `pull_request_review_comment`, `issues`, `pull_request`,
`check_suite`, `check_run`, and `status` events, and give it a secret.

## Naming the repositories

Keep the deployment's repository list in its machine configuration,
`~/.config/openengine/engine.toml` (or
`$XDG_CONFIG_HOME/openengine/engine.toml`). Repository additions do not
require a commit to OpenEngine.

Configuration selection is: explicit `--config`, then `ENGINE_CONFIG`, then
the machine file, then `engine.toml` in the working directory. Files are
selected as a whole, never merged.

When migrating an existing deployment, copy its configuration to the machine
location and move its `.env` alongside it. Update relative paths such as
`workflows.directory` and state locations to preserve their existing targets.
An explicit service config path still takes precedence; update that path if
it points to the checkout. Restart the service after configuration changes.

In the machine file, name the repositories whose deliveries it accepts:

```toml
[repos."owner/name"]
path = "~/code/name"
default = true # default checkout for chat-started work

[repos."other/project"]
path = "~/code/project"
trusted = true

[repos."private/local"]
path = "~/code/local"
mode = "disconnected"
auto_approve_green = false # overrides [pull_requests].auto_approve_green
webhooks = false
```

Every repository with an `owner/name` key accepts webhooks by default. Set
`webhooks = false` to opt out. Local names such as `[repos.n8n]` never accept
webhooks. Names are case-insensitive. Each table requires `path`; optional keys
are `mode`, `trusted`, `auto_approve_green`, `webhooks`, and `default`. Unknown
keys fail startup. At most one repository may set `default = true`.
Those paths select the checkout for work started by assignments, PR mentions,
and review requests.

For one release, legacy string paths and the `[repo_modes]`, `[trusted_repos]`,
`[pull_requests.repos]`, `github.repository`, `github.repositories`, and
`work_orders.repository` settings still load. The side settings log deprecation
warnings and will be removed in the next release. Conflicting legacy and new
settings fail startup. The old GitHub list no longer excludes other configured
repositories: use `webhooks = false` to exclude them. A legacy work-order default
matches by repository name or resolved checkout path; an unmatched path retains
its previous behavior. Plain string paths remain accepted silently.

All repositories use the same `work_orders.workflow` default (or the sole
available workflow) and the same webhook secret. Configure that secret on each
repository's webhook, pointing them all at the same endpoint.

Deliveries from other repositories are
### Choosing the workflow

`work_orders.workflow` names what an assignment or an @mention starts. Either
kind of workflow may be named, and the name is resolved in this order:

1. A Python workflow from `[workflows] directory`, by its id. A deployment that
   already names one keeps it, even when a graph of the same name is also
   registered.
2. A graph registered with `oe graph add`, by name (or `name@N`): first in
   the project named after the repository the work is for, such as `acme/api`,
   then in the `default` project, where `oe graph add` puts a graph when no
   project is named. A graph registered only in some other project is not used
   for this repository; name it by its graph or version id (`g-...`, `gv-...`)
   to run it anyway.

```toml
[work_orders]
workflow = "tdd-guide"
```

A registered graph is started on the same path `oe graph run` takes in the
daemon, and is given what the webhook knows as the inputs it declares:
`instruction` is the task (for an assignment, the issue's title, body and URL)
and, for an assignment, `issue` is the issue's number. A mention on a pull
request leaves `issue` at its default, since a pull request is not an issue to
work. A graph that declares neither input still runs with the task. A Python
workflow gets the task and its own defaults, as before.

A name that matches neither kind, for any repository in `[github]
repositories`, stops `engine-web` and `engine-web --check` at startup with a
configuration error naming it and the repositories it does not resolve for. Without `work_orders.workflow`,
the sole workflow on offer is used, registered graphs included.

The slug format is validated at startup. Deliveries from other repositories are
acknowledged and ignored, even when signed with the same secret. Repository
names are compared without regard to case. Without a configured repository,
actionable comment deliveries receive 503 until setup is complete.

## Storing the secret

The webhook's shared secret is not written in `engine.toml`. Store it as `ENGINE_GITHUB_WEBHOOK_SECRET=your-secret` in a
server-local `.env` beside the loaded `engine.toml` (or in the working
directory when no config file is loaded) — the same file the
[GitHub login](github-login.md) secret uses. It is gitignored; restrict its
permissions to the service owner (`chmod 600 .env`) and edit it over SSH. The
app reads it directly, with dotenv interpolation disabled, without sourcing it
into a shell. A process environment variable of the same name takes precedence,
and the file is reread per delivery, so rotating the secret in GitHub's webhook
settings and on disk takes effect without a restart. Secrets are not accepted
in TOML.

`engine-web --check` reports both halves — the repositories and whether a secret
is readable — so a half-finished setup is visible before the first delivery
arrives.

## The account Engine posts as

Engine resolves its account using its authenticated GitHub credentials. That
identity is used to match issue assignments and ignore Engine's own comments
and merges. No bot-login environment variable is needed. A GitHub app is also
recognised by its user type. The posting account must have write access to every
configured repository, including repositories in other organizations.
`engine-web --check` checks access separately for each repository and reports
missing write access or a failed lookup by name. Failed lookups may indicate
missing credentials, installation access, or a connectivity problem.

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

A verified delivery is queued and acknowledged immediately, because GitHub gives
a webhook ten seconds before it considers the delivery failed. Each comment is
identified by repository, event type, and comment ID and handled once no matter how often GitHub redelivers it.

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
  request opened by hand has no such run: only comments that explicitly
  @mention Engine's GitHub account are processed. Other comments are ignored
  without a reply. An actionable mention starts a work order for the
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

## Starting work from an issue assignment

Subscribe the webhook to `issues`. Engine resolves its account from its
authenticated GitHub credentials.
If no login can be resolved, Engine logs a warning and returns 503 so the
delivery can be retried. Assigning an open issue to that account
starts a work order using `work_orders.workflow`, or the sole available workflow
when no default is configured. The issue title, body, and URL become the task,
including an instruction to close the issue in the resulting PR body.

Only `assigned` events targeting the resolved account are accepted; matching
is case-insensitive. The assigning user must have repository write access.
Ordinary issue comments, other assignees, and closed issues do not start work.
The run uses the issue's repository, without claiming a pull request. No
concierge model turn is needed: assignment itself requests implementation.

Progress is reported on the issue rather than in Slack, as three milestones
only: implementation starting (again, if review sends the work back), review
finishing, and the work order finishing, which follows the pull request's merge
answering its human review. The first two are named by node: a node with id
`implementation` starting, and one with id `reranker` finishing. A registered
graph without such a node -- `tdd-guide` has `reranker` but no
`implementation` node -- posts only the milestones it has nodes for. The pull
request claim, and a merge accepting a `human` node's review, work the same for
a registered graph as for a Python workflow. Other stages, approvals, failures, resumes, and
agent transcript text stay on the work order, both to keep the issue quiet and
because the issue may be public. Each comment links to the work order when
`public_url` is set, and to the pull request once the run has opened one.

Repeated assignments and redeliveries for the same issue are deduplicated in
the bounded ingress memory. Failed handling can be retried by redelivery. As
with comment ingress, this deduplication does not survive a process restart.

## Merging as the human review

A work order stops before it finishes and waits for a person to accept or
reject what it did. Merging its pull request is that acceptance: the run's
human review is approved, exactly as pressing Accept on the WorkOrder page
would have done, and the run carries on. Somebody who has read the diff and
merged it has reviewed the run, and being asked to say so again in another tab
is being asked for a click that says nothing new. Rejecting is still the web
UI's: closing a pull request without merging says the work was abandoned, not
that it was judged.

Merging rather than approving, because merging is the one event that closes
out a work order's pull request. An approving review means one reviewer signed
off, but the branch can take more commits and another round of review after
that, so it is not the point the work is done.

Only a person's merge counts. A bot's is ignored, because the gate exists to
make somebody look at the diff and write access is not that property: a merge
queue, an auto-merge that fires when CI turns green, a Dependabot-style app,
and Engine's own GitHub App all hold write access and none of them has read
anything. A merge GitHub attributes to no account at all is ignored with them.
What is left is a person, and GitHub only accepts a merge from one who can
write to the repository — the same permission a commenter has to hold — so the
merge is its own proof of it.

A merge with no verdict to record is acknowledged and ignored: a pull request
opened by hand, and one whose work order has already stopped. So is a review
already decided in the web UI a moment earlier.

A pull request merged while its work order is still working towards its review
does not skip that step. The run still reaches its review and it is shown as
usual; the merge that already answered it is recorded as soon as it does. The
merge is held in memory until then, so a restart in between leaves the review
for the web UI.

## Waking the CI step

A run's `ci` step waits for its pull request's CI to settle. It reads CI once
when it starts, so CI that finished first is not waited on, and again whenever
a `check_suite` or `check_run` completes, or a commit `status` leaves
`pending`, on the pull request's head commit. That includes a commit pushed
since it last read CI: a check names the pull requests its commit heads, and a
commit `status`, which names none, wakes every step waiting in that
repository. A delivery only says to look
again: the verdict always comes from reading the pull request's required
checks through the API, since one delivery may not cover every check, and
several apps mean several suites for one commit.

The route hands the delivery to whichever steps in this process are waiting on
that repository and commit or pull request. A delivery nobody is waiting on, or from a
repository this deployment does not answer, is acknowledged and ignored. The
waiting steps are held in memory only while they wait.

Without these events, because the webhook is not subscribed to them, a
delivery was dropped, the process restarted, or there is no reachable
`public_url`, the step still reads CI every five minutes until it settles or
the step's hour runs out. A missed delivery costs at most that interval.

## Reading the log

Every delivery that gets past the signature leaves a timestamped line saying
what became of it, because GitHub is answered `200` whether a comment was
queued, ignored, or is still waiting, and its delivery log cannot tell those
apart:

```text
queued comment 5918660346 by octocat on owner/name#614 (delivery 4f0c…); 0 ahead of it
handling comment 5918660346 by octocat on owner/name#614
handled comment 5918660346 by octocat on owner/name#614 in 12.4s: replied, work order run-…
```

A comment that is not acted on says why: `ignored … already queued or
handled`, `ignored … this deployment answers …`, an edit or a bot's comment
(`not a comment Engine acts on`), or a `handled … : ignored, <reason>` naming
the concierge's reason. The delivery id matches the one in GitHub's delivery
log.

One worker handles deliveries in order, so a handler that never returns holds
up every comment behind it. While one runs past a minute the log repeats
`still handling … after Ns; N GitHub deliveries are waiting behind it`. If that
line keeps growing, the process is wedged on that delivery: the comments
behind it were acknowledged and are held only in memory, so restarting loses
them and they need redelivering from GitHub's delivery log.

`engine-web` logs Engine's own messages at INFO and everything else at
WARNING.

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

## Issue references and review replies

An issue assignment carries the issue repository and number as run data. Engine
adds `Refs #N` to workspace commits and snapshots (or `owner/repo#N` for an issue
in another repository). `open_pull_request` requires `issue_resolution` for these
runs: `refs` keeps the issue open; `resolves` also adds `Resolves #N` to the head
commit. Engine normalizes the PR body's issue line for either choice, including
an agent-written `Fixes #N`. The closing keyword takes effect when merged to the
default branch.

Force pushes require an open PR recorded to the current work order, whose head
is in the same repository and has an `agent/` or `feature/` branch name. GitHub
must confirm that the branch is unprotected and is neither the repository's
default branch nor the base of an open PR. Unknown ownership or
unavailable branch information denies the push. Approval does not override
these checks. This applies to explicit force flags and `+` refspecs; GitLab
force pushes are refused until it can verify the same conditions.

Issue publication requires a clean checkout with the current head already
pushed. When the head's issue trailers need updating, Engine appends an empty
metadata commit and pushes it normally before creating the PR. This preserves
published commits and works for the first PR without granting force-push
permission or recording a PR that does not yet exist. A correctly prepared
head needs no extra commit. Concurrent remote updates are never overwritten;
failed pushes reconcile the remote before restoring the local ref, preserving
concurrent local work and uncertain remote state. The PR is recorded to the
work order after GitHub successfully creates it.

Review feedback includes the root comment ID and GraphQL thread ID. Both IDs,
and each thread's resolution state, are also available through
`view_change_request`. For an ordinary review reply through `add_comment`, supply
`pr_url`, `comment`, and `in_reply_to_id`. The resolution options are optional:
`resolve` defaults to `false`, and neither `thread_id` nor `commit_sha` is
required. Ordinary replies leave the thread open and do not pass through the
resolution approval gate. Use this form for discussion or disagreement.

For addressed work, supply `in_reply_to_id`, `thread_id`, `resolve=true`, and a
`commit_sha` of 7–40 hexadecimal characters, along with `pr_url` and `comment`.
Engine requires approval for this operation, validates that the thread belongs
to the PR and matches the root comment, formats `Addressed in <sha>: <comment>`,
posts the reply, then resolves the thread through the configured GitHub
transport. Missing resolution metadata is rejected for `resolve=true` replies.
Retrying the same addressed reply after a resolution failure reuses the posted
reply.

`[github] resolve_addressed_threads = false` leaves addressed threads open for
reviewers to resolve themselves. The default is `true`. Disconnected runs do
not expose review replies or resolution.

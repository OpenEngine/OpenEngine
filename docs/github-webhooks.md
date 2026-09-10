# GitHub comment webhooks

Engine reads comments from GitHub over a signed webhook. Point a GitHub app or a
repository webhook at `<public_url>/api/github/events`, subscribe it to the
`issue_comment` and `pull_request_review_comment` events, and give it a secret.
Start Engine with the same secret:

```bash
GITHUB_WEBHOOK_SECRET=... GITHUB_BOT_LOGIN=... uv run engine-web
```

`GITHUB_BOT_LOGIN` is the GitHub account Engine posts as, whose own comments are
never answered. Set it whenever Engine authenticates with a personal access
token belonging to a machine user: such an account is an ordinary user and
usually a collaborator, so without this it would answer itself in a loop. A
GitHub app is recognised by its user type and needs no setting.

The route verifies the `X-Hub-Signature-256` GitHub sends and refuses anything
it did not sign. A signature only proves GitHub sent the delivery, so authorship
is checked separately: only comments from a repository's owners, members, and
collaborators are acted on, and a bot's own comments are ignored so Engine does
not answer itself.

A verified comment is queued and acknowledged immediately, because GitHub gives
a webhook ten seconds before it considers the delivery failed. Each comment is
handled once no matter how often GitHub redelivers it.

The route answers 503 while no secret is set, while nothing is wired to answer a
comment, or while the queue is full. In each case the comment is not lost: the
delivery stays visible as failed in the webhook's delivery log and can be
redelivered. If handling a comment fails after the delivery was acknowledged,
the comment is forgotten rather than remembered, so redelivering it from the
delivery log picks the work back up. Events Engine does not read, and the `ping`
GitHub sends when the webhook is saved, are always acknowledged, so a webhook
subscribed to more than Engine needs does not accumulate failed deliveries.

# GitHub comment webhooks

Engine reads comments from GitHub over a signed webhook. Point a GitHub app or a
repository webhook at `<public_url>/api/github/events`, subscribe it to the
`issue_comment` and `pull_request_review_comment` events, and give it a secret.
Start Engine with the same secret:

```bash
GITHUB_WEBHOOK_SECRET=... uv run engine-web
```

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
redelivered. The `ping` GitHub sends when the webhook is saved is always
answered, so the webhook can be configured before the rest is in place.

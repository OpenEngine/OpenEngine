---
title: GitHub
---

OpenEngine opens the pull request, and the rest of the review happens on it. Reviewers post inline comments, and impact analysis posts a Green, Orange or Red rating with its reasoning.

<div className="placeholder">Screenshot: reviewer comments and the impact rating on a pull request</div>

- **Comment to steer.** A request on the pull request goes to the WorkOrder working on it. If none is running, a new one starts.
- **Assign to start.** Assign an issue to the bot account and it becomes a WorkOrder that closes the issue.
- **Merge to approve.** A person merging the pull request approves the WorkOrder's human review.

Only comments from owners, members and collaborators are acted on. Merges by bots and merge queues don't count as approval.

```toml title="engine.toml"
[github]
repository = "owner/name"
```

```bash title=".env (beside engine.toml, never committed)"
ENGINE_GITHUB_WEBHOOK_SECRET=...
```

Connect GitHub from the Settings panel, then point a webhook at `<public_url>/api/github/events` for `issue_comment`, `pull_request_review_comment`, `issues` and `pull_request`. Full setup: [github-webhooks.md](https://github.com/OpenEngine/OpenEngine/blob/main/docs/github-webhooks.md).

## Automatically approve Green pull requests

Opt in at the daemon level in `engine.toml` (off by default):

```toml
[pull_requests]
auto_approve_green = true

# Optional overrides, using repository names declared in [repos]:
[pull_requests.repos]
"owner/name" = false
```

Restart the daemon after changing this setting. Existing registered default graph
versions need to be updated by registering the new workflow before starting new
runs; an already pinned version keeps its original nodes. The default implementation/review
workflow and the standalone review graph submit an APPROVE review after a Green
impact rating, including its rationale. Orange and Red ratings never qualify.
A repository override wins over the global setting; disconnected repositories
and disconnected runs never qualify. Failed, pending or unavailable CI and
unresolved reranked findings or review threads block approval. CI is checked
again immediately before approval, including for review-only runs.

The daemon's forge identity must differ from the PR author: GitHub refuses
self-approval. Use this for reviewing someone else's PR or with a separate
bot/app identity that has review permission. Self-authored PRs are skipped with
“cannot approve own pull request”; provider errors are also surfaced as skips.
Every decision appears in the auto-approve node's events and transcript, visible
in the web UI and `engine nodes`, and in daemon logs.

This setting only adds a forge approval. It never merges a PR or bypasses the
WorkOrder's human acceptance step. GitLab uses a merge request approval and a
separate note containing the rationale.

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

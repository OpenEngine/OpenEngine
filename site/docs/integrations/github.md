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

## Requested reviews

Request Engine's account as a reviewer to start the dedicated `review` graph.
It posts surviving findings as review comments and a summary with the impact
rating and rationale. Requests on forks or PRs with an active WorkOrder are
skipped, and the requester must have write access.

```toml title="engine.toml"
[github]
review_request_workflow = "review"

[pull_requests]
auto_approve_green = false

# Optional overrides, keyed by existing [repos] names:
[pull_requests.repos]
"owner/name" = true
```

Configuration supplies defaults when each run starts. A repository override
wins over the global value; an explicit `auto_approve_green` run input wins
over both. In the web form, leave that input empty to use configuration, or
choose `true` or `false` to override it.

The graph shows publication, the approval check, and the conditional approval
path. Automatic approval requires Green impact, no surviving findings, a PR
URL, connected mode, publishing enabled, and CI no worse than pending or
neutral. Failed CI blocks approval. Engine skips self-approval with a visible
reason. It never merges the PR.

`engine go review <PR>` keeps its output local by default. To publish and
explicitly enable Green approval:

```bash
engine go review <PR> -i publish=true -i auto_approve_green=true
```

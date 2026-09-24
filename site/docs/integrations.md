---
title: Integrations
description: Start, follow and approve work from Slack and GitHub, without opening OpenEngine.
---

Start, follow and approve work from Slack and GitHub, without opening OpenEngine.

## Slack

Mention `@OpenEngineBot` in a channel and ask for a change. It starts a WorkOrder and the thread becomes its home: progress, questions and the review request all post there.

![A Slack message mentioning @OpenEngine with a failing CI log, asking it to resolve the failure](/img/screenshots/slack-mention.png)

![The OpenEngine bot reporting a completed review, then resuming the WorkOrder when asked to change direction](/img/screenshots/slack-thread.png)

- **Ask about the code.** The bot can read your repository and answer questions without starting work.
- **Steer in the thread.** Reply with a correction and it reaches the agent that's working.
- **Answer questions.** When an agent needs input, the question and its choices post in the thread.
- **Decide in the thread.** Reply `approve` or `request changes: …` when the review is ready.
- **Follow up.** Ask for more fixes after it finishes and the same WorkOrder resumes.

Only the person who started a WorkOrder, and anyone listed in `slack_operators`, can steer or decide it. Everyone else can join the discussion.

```toml title="engine.toml"
public_url = "https://engine.example"

[communications]
provider = "slack"
channel = "C0123456789"

[work_orders]
workflow = "implementation-review-rerank"
slack_operators = ["U01234567"]
```

Connect the app in **Settings → Slack**, then point Slack's Events URL at `<public_url>/api/slack/events`. Full setup: [work-orders-from-slack.md](https://github.com/OpenEngine/OpenEngine/blob/main/docs/work-orders-from-slack.md).

## GitHub

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

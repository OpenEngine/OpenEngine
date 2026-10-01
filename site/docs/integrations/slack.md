---
title: Slack
---

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

---
title: WorkOrders
description: A WorkOrder is one task run through a workflow. It is where the actual work happens.
---

A WorkOrder is one task run through a workflow. It is where the actual work happens.

Each WorkOrder gets its own git worktree and branch, and ends with a pull request waiting on your decision. Its page shows every stage, each agent's conversation, and anything waiting on you.

![A WorkOrder awaiting human review, with its stages and the Approve and Reject buttons](/img/screenshots/workorder-page.png)

## Creating a WorkOrder

| From | How |
| --- | --- |
| Web UI | **Create a WorkOrder**: choose a workflow and repository, then describe the task. |
| Slack | Mention `@OpenEngineBot` and ask for the change. |
| GitHub issue | Assign the issue to the OpenEngine bot account. |
| Pull request | Comment asking for a change. A running WorkOrder is steered; otherwise a new one starts. |
| MCP | Call `create_workorder(prompt)` on the [remote MCP server](https://github.com/OpenEngine/OpenEngine/blob/main/docs/remote-mcp.md). |

![The Create a WorkOrder form, with Codex implementing and Claude reviewing](/img/screenshots/workorder-create.png)

Describe what should change and what success looks like. The task is the implementation agent's entire brief.

## Use two model families

Implement with one model family and review with another. A model reviewing its own work tends to agree with itself. A different family has different blind spots and catches what the first one missed.

The shipped workflow does this by default: Codex implements, Claude reviews. Choose either under **Workflow inputs** with the **Implementation runner** and **Review runner** fields.

| Review runner | Most facets | Security |
| --- | --- | --- |
| Claude | `sonnet` | `opus` |
| Codex | `gpt-5.6-terra` | `gpt-5.6-sol` |

Security reviews use the larger tier, since a missed finding there costs the most.

## While it runs

- **Steer.** Message the agent that's working. Your message lands in its current turn.
- **Approve.** Tool calls your approval policy doesn't cover wait for you in the conversation.
- **Decide.** At human review, approve or request changes. Merging the pull request also counts as approval.
- **Resume.** Ask for follow-up fixes after it finishes. The same branch and conversation continue.

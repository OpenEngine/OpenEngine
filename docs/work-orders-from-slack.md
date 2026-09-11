# Slack concierge and work orders

Mention `@OpenEngineBot` to open a conversation. A greeting or test message gets
“Hi, how can I help?”. Ask for a new work order in that thread and the concierge
uses its `create_workorder` tool. The host starts the configured step workflow,
posts its UI link, and reports progress in the same thread.

## Setup and diagnosis

1. Connect the Slack app in Settings → Slack and save its signing secret.
   A missing secret returns HTTP 503; an invalid signature returns 401. A missing
   bot token is logged and the delivery is ignored because no reply can be sent.
2. Set the Slack Events request URL to `<public_url>/api/slack/events` and
   subscribe to `app_mention`. Invite the bot to the channel.
3. For replies without another mention, subscribe to `message.channels`, and to
   `message.groups` as well for private channels. OpenEngine's authorization
   request already asks for the `channels:history` and `groups:history` scopes
   those events require, so a workspace connected before they were requested has
   to be reconnected in Settings → Slack for Slack to start delivering them. See
   Slack's
   [message event documentation](https://docs.slack.dev/reference/events/message/).
4. Install the Codex ACP adapter prerequisites (`npx` and Codex authentication).
   The web composition defaults to `CodexACPProvider`; tests or another deployment
   can inject an `ACPAgentProvider` through `create_app(concierge_provider=...)`.
   The concierge uses an isolated temporary directory, not a repository checkout.

```toml
public_url = "https://engine.example"

[work_orders]
repository = "."                        # local checkout path; defaults to "."
workflow = "implementation-review-v1"   # optional if exactly one is installed
runner = "claude"                       # work-order executor, not concierge
slack_operators = ["U01234567"]          # may control work started by others
```

A greeting needs no work-order configuration. Creating work requires a resolvable step workflow. The repository is a local
checkout path, not a GitHub owner/name, and cannot be supplied by the agent. Missing configuration becomes a tool error so the
concierge can explain what is needed.

## Code boundaries

`packages/slack-concierge` installs `engine-slack-concierge`:

- `slack_concierge.py`: `build_graph()` defines `START → concierge → reply → END`.
  The conversation node uses the `langgraph-acp` client/session API to inject MCP
  tools and reuse a live session. `handle(IncomingMessage)` runs a turn;
  `has_thread`, `forget`, and `close` manage its lifetime.
- `slack_ingress.py`: `webhook()` verifies the signed request and acknowledges
  after enqueueing. `accept()` routes mentions and replies in known threads,
  ignores bots and message edits, and deduplicates by channel/message timestamp
  across both Slack event types. `drain()` and `close()` support tests/shutdown.
- `slack_egress.py`: the granted MCP tools are `create_workorder(prompt)` and,
  when the host supplies their callbacks, `steer_workorder(prompt)` and
  `resume_workorder(prompt)`, `answer_workorder_question(approval_id, answers)`,
  and `decide_workorder_review(approved, summary)`.
  Their host callbacks return `(url, run_id)`.
  The host binds the Slack origin; the model cannot supply a destination channel
  or thread. The stdio-to-TCP bridge keeps its credential in a mode-0600 temporary
  file and advertises a fixed supported MCP protocol version.

The web composition supplies the work-order callback and thread reply callback.
It reuses `start_step_run` and `RunNotifier` for execution, links, and progress.
The concierge no longer shares `AgentSession` state or appears as a chat profile.

Concierge sessions and delivery deduplication are process-local. The concierge keeps
at most 32 live sessions, evicting and closing the least recently used session;
failed turns also close their session. Turns are serialized and limited to 180
seconds. The queue holds 256 messages and rejects overflow with HTTP 503 so Slack
can retry. Threads without a WorkOrder need a new mention after eviction or a
server restart. Threads with a stored WorkOrder are recognized from its origin,
including after completion, and ordinary replies can open a fresh concierge
session. Each turn receives the linked WorkOrders' current state, task, sender,
and original mention markup; this is not a restoration of the full Slack history.
Work-order records remain in the existing persistent store, and their status updates keep
the original thread even after its concierge session is evicted. This first
version is intended for a single web process; durable ingress and multi-worker
conversation routing are future work.

## What the agent can say

A step whose run came from a conversation is served one extra run-bound MCP
tool, `update_status`, and told to use it. It takes a sentence, posts it in the
thread, and does not end the step. A run started from the web is not served the
tool at all, because there is nowhere for its updates to go.

Everything else in the thread is the runtime reporting, not the agent:

| What happened | What the thread says |
| --- | --- |
| A step began | `*Review* started.` |
| `update_status` | `*Implementation*: <what the agent wrote>` |
| `complete_step` | `*Implementation* complete.` with the step's summary, the pull request when the step declared a `pr_url` output, and a link to the WorkOrder |
| `fail_step` | `*Implementation* failed.` with the reason |
| `clarify` | the agent answered a question and changed nothing |
| Any other pausing tool | `*Implementation* is waiting for an answer.` with what it asked |
| The run died outside a step | `This work order failed.` with the reason |
| Reviews finished | the review is complete and waiting on them, addressed by name |

That last one does not depend on the workflow's `notification=`, which says
whether to *also* announce in the operators' channel — a different message with
a different audience. A run started from a conversation always gets its ping,
or the thread would report the review complete and then go quiet with the run
parked on a decision nobody was told about.

Thread replies continue the concierge conversation. A correction to running work
can use `steer_workorder`: the host selects the sole linked WorkOrder and passes
the instruction to its current editable agent conversation. Slack and web
continuations share a per-WorkOrder lock. The instruction records the current
Slack sender, even when somebody else opened the thread. Receipt is acknowledged
only after the human message is recorded; implementation progress follows later.
Read-only steps, paused or completed work, pending approvals, and ambiguous
threads are refused without creating another WorkOrder.

The person who started a WorkOrder can steer it, resume it, answer its structured
questions, and make its review decision. A deployment can additionally grant
those controls to Slack user IDs in `work_orders.slack_operators`; all other
thread participants can discuss the work but their replies cannot invoke a
control tool. Starting a new WorkOrder remains available to any user who can
mention the bot.

Structured user-input requests are posted as `Input required` with their question
text and choices. The concierge receives current pending question IDs on every
turn and can submit the human's answers through `answer_workorder_question`.
The host verifies the Slack thread, WorkOrder, current agent run, request type,
and pending status before using the same answer service as the web UI. Choice,
multi-select, and completeness validation stays in that service. The waiting
provider turn continues without being restarted. Questions already answered in
the UI, interrupted questions, and tool permission requests cannot be answered
through this tool. A lost concierge session does not lose a still-live question;
a server restart that ends the provider turn does not restore that waiting turn.

The review-ready message tells the thread exactly how to respond: `approve` or
`request changes: <feedback>`. An explicit reply that approves a pending review
or requests changes can use
`decide_workorder_review`. The host verifies that this exact Slack thread has one
WorkOrder and that it is still awaiting human review, then records the same
`HumanReviewCompleted` event as the WorkOrder page. A request for changes must
include feedback. The concierge must not infer a review decision from ordinary
discussion, questions, or status checks. Tool permission approvals and legacy
clarification tools that end the agent turn without a structured pending question
still require the WorkOrder page.
The create tool refuses to create another WorkOrder in a thread already linked
to work, even after that work completes. Start a separate Slack thread for a new
task. For follow-up fixes after completion, failure, or while awaiting human
review, `resume_workorder` reuses the existing WorkOrder's unique editable,
write-enabled implementation conversation. The normal workflow reactivation
rules retain its history and workspace and run subsequent stages again. It does
not approve or merge a PR. Paused agents still require the WorkOrder page, and a
second resume is refused while execution is already in progress. Missing or
ambiguous implementations and detached workspaces are refused before restarting.
This uses the same workspace/source-control behavior as web continuation; it does
not yet detect merged PRs or provision a replacement workspace automatically.
If multiple older WorkOrders
share a thread, all are supplied as context and creation remains blocked rather
than silently choosing one. The lookup currently scans stored runs for this
single-workspace deployment; a dedicated index can replace it as volume grows.

## What it will not do

- **`[BETA]` graph workflows are not startable this way.** They are run by the
  other engine, which has neither the run-bound tools an agent reports through
  nor anywhere to keep where the request came from — so one started from a
  mention would go silent the moment it began. Only step workflows are offered.
- **Duplicate deliveries are ignored while remembered.** Accepted message identities
  are bounded to 4096 entries. A retry that was never accepted can be processed;
  no cross-restart exactly-once guarantee is claimed.
- **Nothing is reported when the provider is down.** A Slack outage must not
  fail the work it was reporting on, so the runtime's own messages are best
  effort: they are logged and dropped, and the run continues with its record on
  the WorkOrder page unaffected. `update_status` is the exception, because the
  agent is waiting on the answer to its own tool call — it is told the status
  did not go out, which does not end the step either. A disconnected workspace
  counts as down: it is reported, not treated as a message that was sent.

# Slack concierge and work orders

Mention `@OpenEngineBot` to open a conversation. A greeting or test message gets
“Hi, how can I help?”. Ask for a new work order in that thread and the concierge
uses its `create_workorder` tool. The host starts the configured workflow,
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
workflow = "implementation-review-codex"   # optional if exactly one is installed
```

A greeting needs no work-order configuration. Creating work requires a resolvable workflow. The repository is a local
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
- `slack_egress.py`: the only granted MCP tool is
  `create_workorder(prompt)`. Its host callback returns `(url, run_id)`.
  The host binds the Slack origin; the model cannot supply a destination channel
  or thread. The stdio-to-TCP bridge keeps its credential in a mode-0600 temporary
  file and advertises a fixed supported MCP protocol version.

The web composition supplies the work-order callback and thread reply callback.
It uses the graph or step start path and `RunNotifier` for links and announcements.
Graph workflows select their own runner; `work_orders.runner` applies to step workflows.
The concierge no longer shares `AgentSession` state or appears as a chat profile.

Conversations and delivery deduplication are process-local. The concierge keeps
at most 32 live sessions, evicting and closing the least recently used session;
failed turns also close their session. Turns are serialized and limited to 180
seconds. The queue holds 256 messages and rejects overflow with HTTP 503 so Slack
can retry. An evicted thread or server restart needs a new mention. Work-order
records remain in the existing persistent store, and their status updates keep
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

Thread replies continue the concierge conversation. Work-order approval or
clarification questions are still answered on the WorkOrder page.

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

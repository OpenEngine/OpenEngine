# `langgraph-acp` Architecture and Implementation Plan

## Purpose

`langgraph-acp` makes ACP-compatible agents usable as first-class LangGraph nodes.

The package should allow a LangGraph workflow to invoke an ACP-backed agent such as Codex or Claude while preserving the capabilities that make ACP useful:

- Start and resume ACP sessions.
- Preserve agent conversation state across LangGraph invocations.
- Stream ACP messages, tool calls, plans, usage, and session updates into LangGraph.
- Handle live approval and elicitation requests without restarting the running node.
- Dynamically provide MCP servers and tools to an agent at runtime.
- Control permission behavior for tools and operations.
- Configure ACP sessions using the capabilities advertised by the agent.
- Propagate cancellation and manage session lifecycle.
- Keep the adapter generic enough to work across ACP-compatible agent implementations.

The intended simple public API is:

```python
ACPNode(agent="codex")
ACPNode(agent="claude")
```

More advanced workflows should be able to configure the node dynamically:

```python
review_agent = ACPNode(
    agent="codex",

    prompt=lambda state: state["review_prompt"],

    session=ACPSession(
        strategy="reuse",
        key=lambda state, ctx: f"pr:{state['pr_id']}:reviewer",
    ),

    workspace=ACPWorkspace(
        cwd=lambda state: state["workspace_path"],
        additional_directories=lambda state: [
            state["shared_docs_path"],
        ],
    ),

    mcp_servers=lambda state, ctx: [
        MCPServer.stdio(
            name="github",
            command=["github-mcp-server"],
        ),
        MCPServer.http(
            name="workflow",
            url=state["workflow_mcp_url"],
        ),
    ],

    permissions=PermissionPolicy(
        rules=[
            PermissionRule.allow("read"),
            PermissionRule.allow("search"),
            PermissionRule.ask("write"),
            PermissionRule.ask("shell"),
        ],
        default="ask",
    ),

    config=ACPConfig(
        by_category={
            "mode": "code",
            "thought_level": "high",
        },
    ),
)
```

The important architectural boundary is:

```text
LangGraph owns orchestration and workflow durability.

ACP owns the agent conversation and agent-side session state.

langgraph-acp owns the binding between the two.
```

---

# Top-Level Objects

## `ACPNode`

`ACPNode` is the primary public primitive.

It is a LangGraph-compatible callable/node that:

1. Resolves its runtime configuration from LangGraph state and context.
2. Determines which logical ACP session should be used.
3. Looks up the opaque ACP session ID.
4. Connects to the configured ACP agent.
5. Starts or resumes the ACP session.
6. Sends the prompt.
7. Streams ACP updates into LangGraph.
8. Handles permission and elicitation requests while the prompt remains active.
9. Collects usage and final result information.
10. Returns an `ACPResult`.

Conceptually:

```text
LangGraph invocation
       │
       ▼
    ACPNode
       │
       ├── resolve agent
       ├── resolve session
       ├── resolve workspace
       ├── resolve MCP servers
       ├── resolve config
       │
       ▼
 ACP session/new
       or
 ACP session/resume
       │
       ▼
 session/prompt
       │
       ├── events
       ├── permission requests
       ├── elicitation requests
       └── usage
       │
       ▼
   ACPResult
```

Most constructor options should support either static values or runtime resolvers.

For example:

```python
ACPNode(
    mcp_servers=lambda state, ctx: state["agent_tools"]
)
```

This allows LangGraph to decide which capabilities an agent receives for each workflow or task.

---

# `ACPAgentProvider`

`ACPAgentProvider` describes how to reach a particular ACP-compatible agent implementation.

For example:

```text
codex
claude
gemini
opencode
```

The provider is responsible for things such as:

- Executable or process configuration.
- Transport setup.
- Authentication/environment setup.
- ACP initialization.
- Implementation-specific compatibility behavior.
- Agent capability discovery.

Example conceptual interface:

```python
class ACPAgentProvider(Protocol):
    async def connect(self) -> ACPConnection: ...
```

`ACPNode` should not contain Codex-specific or Claude-specific logic.

---

# `ACPAgentRegistry`

The registry resolves a friendly name into an `ACPAgentProvider`.

For example:

```python
registry.register("codex", CodexACPProvider(...))
registry.register("claude", ClaudeACPProvider(...))
```

Then:

```python
ACPNode(agent="codex")
```

can resolve the provider internally.

The registry keeps the public API simple and gives applications a way to register their own ACP agents.

---

# `ACPSession`

`ACPSession` describes how the node identifies and manages a logical agent conversation.

Example:

```python
ACPSession(
    strategy="reuse",
    key="primary-reviewer",
)
```

or dynamically:

```python
ACPSession(
    strategy="reuse",
    key=lambda state, ctx:
        f"{state['repo']}:{state['pr_number']}:reviewer",
)
```

Potential strategies:

```text
new
reuse
resume
```

In practice, `reuse` should probably be the normal default:

```text
binding exists
    → resume ACP session

binding does not exist
    → create ACP session
    → persist returned session ID
```

The important point is that `ACPSession` does not contain conversation history.

The ACP-backed CLI or agent runtime owns that state.

---

# `ACPSessionStore`

`ACPSessionStore` is a typed persistence boundary for the relationship between a LangGraph logical session and an opaque ACP session ID.

Its responsibility is only:

```text
LangGraph logical identity
        ↓
ACP session ID
```

For example:

```text
(thread_id="pr-918", session_key="reviewer")
        ↓
"sess_abc123"
```

It does **not** store:

- Conversation history.
- Agent messages.
- Tool history.
- Model context.
- ACP session memory.

Those remain owned by the ACP implementation.

A minimal interface could be:

```python
class ACPSessionStore(Protocol):

    async def get(
        self,
        thread_id: str,
        session_key: str,
    ) -> str | None:
        ...

    async def put(
        self,
        thread_id: str,
        session_key: str,
        acp_session_id: str,
    ) -> None:
        ...

    async def delete(
        self,
        thread_id: str,
        session_key: str,
    ) -> None:
        ...
```

A slightly richer binding type may be useful:

```python
@dataclass
class ACPSessionBinding:
    thread_id: str
    session_key: str
    agent: str
    acp_session_id: str
```

Possible implementations:

```text
InMemoryACPSessionStore
LangGraphACPSessionStore
SqliteACPSessionStore
PostgresACPSessionStore
```

The type is worth keeping even if the initial implementation simply uses LangGraph checkpoint persistence.

---

# Session Identity

A LangGraph thread can contain multiple ACP-backed agents.

For example:

```text
PR #918

implementation agent → ACP session A

reviewer #1          → ACP session B

reviewer #2          → ACP session C

security reviewer    → ACP session D
```

Therefore the binding cannot simply be:

```text
thread_id → ACP session
```

The logical key should be something like:

```text
(thread_id, session_key) → ACP session ID
```

The default `session_key` could be derived from the LangGraph node name.

Explicit keys should be supported when one node can represent multiple logical conversations.

---

# Webhook Resume Example

This session mapping enables the GitHub comment/reply workflow.

Suppose an ACP reviewer creates GitHub comment:

```text
comment_id = 12345
```

The workflow records:

```text
github_comment_id = 12345
langgraph_thread_id = pr-918
agent_session_key = primary-reviewer
```

Later:

```text
GitHub reply webhook
       │
       ▼
comment 12345
       │
       ▼
thread = pr-918
session_key = primary-reviewer
       │
       ▼
ACPSessionStore
       │
       ▼
sess_abc123
       │
       ▼
ACP session/resume(sess_abc123)
       │
       ▼
agent CLI hydrates its own conversation
```

No transcript reconstruction is necessary.

---

# `ACPWorkspace`

`ACPWorkspace` describes the filesystem context made available to the ACP session.

Example:

```python
ACPWorkspace(
    cwd=lambda state: state["workspace_path"],
    additional_directories=lambda state: [
        state["shared_docs"],
    ],
)
```

It should support:

- Current working directory.
- Additional workspace roots where ACP supports them.
- Runtime resolution from LangGraph state.
- Capability validation.

Workspace configuration belongs at the ACP-session boundary rather than being hard-coded into an agent provider.

---

# `MCPServer`

Runtime MCP augmentation should be a first-class capability.

Example:

```python
MCPServer.stdio(
    name="github",
    command=["github-mcp-server"],
)
```

or:

```python
MCPServer.http(
    name="workflow",
    url="http://localhost:8001/mcp",
)
```

`ACPNode` should accept either:

```python
mcp_servers=[...]
```

or:

```python
mcp_servers=lambda state, ctx: [...]
```

This allows the workflow to give different capabilities to different agents.

For example:

```text
Implementation agent
- filesystem
- GitHub
- browser
- CI
- artifact publishing

Reviewer
- filesystem read
- GitHub read
- browser

Deploy agent
- GitHub
- deployment system
- observability
```

The set of exposed tools should be decided at runtime.

---

# MCP and Permissions Are Different

There are two separate questions:

## What tools exist?

Controlled primarily through MCP exposure.

```text
MCP servers
    ↓
tools visible to agent
```

## What operations require authorization?

Controlled through `PermissionPolicy`.

```text
ACP permission request
    ↓
allow / deny / ask
```

These should remain separate abstractions.

---

# `PermissionPolicy`

`PermissionPolicy` decides how ACP permission requests are handled.

Example:

```python
PermissionPolicy(
    rules=[
        PermissionRule.allow("read"),
        PermissionRule.allow("search"),
        PermissionRule.ask("write"),
        PermissionRule.ask("execute"),
        PermissionRule.deny("delete"),
    ],
    default="ask",
)
```

It should also support application-defined policies:

```python
async def permission_policy(request, state, ctx):

    if request.tool.server == "github":
        if request.tool.name in {"get_pr", "get_diff"}:
            return ALLOW

        if request.tool.name == "merge_pr":
            return ASK

    return DENY
```

A key limitation should be documented clearly:

`PermissionPolicy` governs ACP permission requests.

It cannot universally sandbox arbitrary agent-native tools if the ACP agent implementation does not expose those operations through permission requests.

Injected MCP tools can be controlled more strongly because the application controls which MCP surface is exposed.

---

# `ACPInteractionBroker`

Some ACP requests require interaction with something outside the running node.

Examples:

- User approval.
- Operator approval.
- A question requiring user input.
- Structured elicitation.

The node must be capable of waiting for these responses without restarting the ACP turn.

A general interface could be:

```python
class ACPInteractionBroker(Protocol):

    async def request_permission(
        self,
        request: ACPPermissionRequest,
    ) -> ACPPermissionDecision:
        ...

    async def request_elicitation(
        self,
        request: ACPElicitationRequest,
    ) -> ACPElicitationResponse:
        ...
```

Possible implementations:

```text
InProcessInteractionBroker
WebsocketInteractionBroker
WorkflowInteractionBroker
```

The broker may assign a durable external request ID:

```text
approval_7f83a
```

A UI or API can later resolve:

```python
broker.resolve(
    "approval_7f83a",
    APPROVE,
)
```

---

# Live Approval Behavior

Live approval should not primarily use LangGraph node interruption.

Instead:

```text
ACP agent
    │
    │ permission request
    ▼
ACPNode
    │
    ├── emit permission event
    │
    ▼
ACPInteractionBroker
    │
    │ await external response
    ▼
UI / API / Slack / workflow
    │
    │ approve
    ▼
ACPInteractionBroker
    │
    ▼
ACPNode
    │
    │ ACP permission response
    ▼
ACP agent continues same turn
```

The running ACP request remains alive.

The node is not restarted.

A useful distinction is:

```text
ACP session
    = resumable agent conversation

live permission request
    = active request attached to a running connection
```

If the worker process dies while awaiting approval, the ACP conversation may still be resumable, but the exact pending live permission exchange should not automatically be assumed durable.

---

# Elicitation

ACP may request structured information from the user separately from permission checks.

This should use the same interaction infrastructure.

Example events:

```text
acp.elicitation.requested
acp.elicitation.resolved
```

This is useful when the agent asks a workflow-level question such as:

```text
Which migration strategy should I use?
```

rather than asking permission to execute something.

---

# `ACPConfig`

`ACPConfig` describes desired session configuration.

Example:

```python
ACPConfig(
    by_category={
        "model": "some-model",
        "mode": "code",
        "thought_level": "high",
    },
)
```

It should avoid turning every possible ACP config option into a dedicated Python constructor argument.

Instead, support:

```python
ACPConfig(
    by_category={...},
    by_id={...},
)
```

The adapter should:

1. Read the configuration capabilities advertised by the ACP agent.
2. Match requested semantic categories.
3. Apply supported values.
4. Handle unsupported values according to policy.

For example:

```python
ACPConfig(
    unsupported="error"
)
```

or:

```python
ACPConfig(
    unsupported="ignore"
)
```

---

# Capability Negotiation

ACP implementations will not necessarily expose identical functionality.

`ACPNode` should inspect capabilities during initialization.

Optional requirements could be expressed as:

```python
ACPRequirements(
    resume=True,
    mcp=True,
    elicitation=False,
)
```

If a workflow requires a capability that the configured agent does not support, fail clearly and early.

For example:

```text
ACPAgentCapabilityError

Agent "codex" does not support the required MCP transport.
```

Avoid hiding compatibility failures deep inside a running prompt.

---

# `ACPEvent`

ACP produces a large amount of streaming activity.

Most of this should not be written directly into durable LangGraph state.

Instead, normalize it into streamed events.

Suggested event types:

```text
acp.session.started
acp.session.resumed
acp.session.closed

acp.message.delta
acp.message.completed

acp.thought.delta

acp.tool.started
acp.tool.updated
acp.tool.completed

acp.plan.updated

acp.permission.requested
acp.permission.resolved

acp.elicitation.requested
acp.elicitation.resolved

acp.config.updated
acp.session.info_updated

acp.usage.updated

acp.prompt.completed

acp.error

acp.raw
```

A normalized event could look like:

```python
ACPEvent(
    agent="codex",
    session_id="sess_123",
    thread_id="pr-918",
    node="review",
    type="tool.updated",
    timestamp=...,
    data=...,
)
```

Unknown ACP event types should not crash the adapter.

They should be emitted through:

```text
acp.raw
```

This gives the library forward compatibility.

---

# `ACPResult`

The final node result should be normalized rather than simply returning a string.

Example:

```python
ACPResult(
    message="Reviewed the change.",

    content=[...],

    session=ACPSessionRef(
        agent="codex",
        session_id="sess_abc123",
        key="primary-reviewer",
    ),

    stop_reason="end_turn",

    usage=ACPUsage(...),

    tool_calls=[...],

    metadata={...},
)
```

A convenience option should allow mapping the result into LangGraph state:

```python
ACPNode(
    agent="codex",
    output_key="review",
)
```

So simple workflows can continue accessing:

```python
state["review"]
```

while advanced callers can consume the full `ACPResult`.

---

# Usage

Usage should be normalized and exposed through both events and final results.

Possible fields include:

```text
input tokens
output tokens
reasoning/thought tokens
cache tokens

context used
context size

cost
```

Example:

```python
ACPUsage(
    input_tokens=...,
    output_tokens=...,
    thought_tokens=...,
)
```

Session-level usage should be streamable as it changes.

This gives higher-level orchestration the ability to aggregate:

```text
workflow cost
PR cost
agent cost
review cost
implementation cost
```

without embedding billing logic into the agent runtime.

---

# Cancellation

LangGraph cancellation should propagate into ACP.

Conceptually:

```text
LangGraph task cancelled
        ↓
ACPNode
        ↓
ACP session cancellation
```

Cancellation and session closure must remain separate.

```text
cancel
    = stop current turn

close
    = release/end ACP session
```

Cancelling a node should not destroy a reusable ACP conversation unless explicitly configured to do so.

---

# Secrets

Secrets should never be serialized into:

- LangGraph state.
- LangGraph checkpoints.
- ACP events.
- `ACPResult`.
- Logs.

Runtime MCP configuration should support secret references rather than concrete values.

For example:

```python
MCPServer.stdio(
    name="github",
    command=["github-mcp-server"],
    env={
        "GITHUB_TOKEN": SecretRef("github-token")
    },
)
```

The provider/runtime layer resolves the actual secret immediately before process or transport creation.

---

# Proposed Package Shape

A possible package layout:

```text
langgraph_acp/

    node.py
        ACPNode

    agent.py
        ACPAgentProvider
        ACPAgentRegistry
        ACPConnection

    session.py
        ACPSession
        ACPSessionRef
        ACPSessionBinding
        ACPSessionStore

    workspace.py
        ACPWorkspace

    mcp.py
        MCPServer

    permissions.py
        PermissionPolicy
        PermissionRule
        ACPPermissionRequest
        ACPPermissionDecision

    interactions.py
        ACPInteractionBroker
        ACPElicitationRequest
        ACPElicitationResponse

    config.py
        ACPConfig
        ACPRequirements

    events.py
        ACPEvent

    result.py
        ACPResult
        ACPUsage

    errors.py
        ACPError
        ACPAgentCapabilityError
        ACPSessionError
```

---

# End-to-End Execution

The normal `ACPNode` execution path should look like:

```text
LangGraph enters ACPNode
        │
        ▼
resolve provider
        │
        ▼
connect + ACP initialize
        │
        ▼
inspect capabilities
        │
        ▼
resolve session key
        │
        ▼
ACPSessionStore.get(...)
        │
       ┌┴──────────────────────┐
       │                       │
       ▼                       ▼
 no session                 existing session
       │                       │
       ▼                       ▼
 session/new             session/resume
       │                       │
       └──────────┬────────────┘
                  │
                  ▼
       resolve workspace/MCP/config
                  │
                  ▼
             session/prompt
                  │
        ┌─────────┼───────────┐
        │         │           │
        ▼         ▼           ▼
      events   permissions   elicitation
        │         │           │
        │         ▼           ▼
        │    interaction broker
        │         │
        └─────────┴───────────┐
                              │
                              ▼
                    prompt completes
                              │
                              ▼
                        ACPResult
```

---

# Acceptance Criteria

## Basic invocation

Given:

```python
ACPNode(agent="codex")
```

When the node is invoked for the first time:

- The configured ACP provider is resolved.
- An ACP connection is initialized.
- The agent capabilities are recorded.
- A new ACP session is created.
- The returned ACP session ID is persisted.
- The prompt executes.
- The final response is returned as `ACPResult`.

---

## Session reuse

Given an existing binding:

```text
(thread_id, session_key) → sess_123
```

When the same logical ACP node is invoked again:

- `session/new` is not called.
- `session/resume(sess_123)` is used.
- The agent runtime restores its own conversation state.
- The new prompt is sent into that restored session.

The adapter must not reconstruct or replay conversation history itself.

---

## Multiple agents

A single LangGraph thread can maintain independent ACP sessions.

For example:

```text
(thread, implementer) → session A
(thread, reviewer)    → session B
(thread, security)    → session C
```

One logical agent must never accidentally resume another logical agent's session.

---

## Restart durability

After the LangGraph worker or application restarts:

- The session binding remains available through `ACPSessionStore`.
- The node can resume the ACP session.
- No local transcript reconstruction is required.

---

## Streaming

Before the node completes, consumers can observe:

- Message deltas.
- Tool starts.
- Tool updates.
- Tool completion.
- Plan updates.
- Permission requests.
- Elicitation requests.
- Usage updates.
- Prompt completion.

---

## Runtime MCP

Given MCP servers generated from current graph state:

```python
mcp_servers=lambda state, ctx: [...]
```

When the ACP session is created or resumed:

- The intended MCP configuration is supplied.
- Supported tools become usable by the agent.
- Unsupported configurations fail explicitly rather than silently disappearing.

---

## Workspace

Dynamic workspace configuration is resolved at invocation time.

Supported workspace roots are supplied to ACP.

Unsupported workspace behavior produces a typed capability error.

---

## Automatic permission allow

Given a matching `allow` rule:

- The ACP permission request is answered automatically.
- No external approval is required.

---

## Automatic permission deny

Given a matching `deny` rule:

- The request is rejected.
- The ACP prompt continues according to ACP semantics.

---

## Live permission approval

Given a permission rule that evaluates to `ask`:

- `acp.permission.requested` is emitted.
- The running ACP prompt remains active.
- The interaction broker waits for an external decision.
- The external response is returned to ACP.
- The same ACP turn continues.
- The LangGraph node is not restarted.

---

## Elicitation

When ACP requests structured user input:

- `acp.elicitation.requested` is emitted.
- The interaction broker waits for input.
- The response is returned into the active ACP request.
- The same ACP turn continues.

---

## Usage

When the ACP implementation provides usage information:

- Usage is normalized.
- Usage updates are streamable.
- Final usage is available from `ACPResult`.

---

## Config

Requested configuration options are applied when supported.

Unsupported options follow configured behavior such as:

```text
error
ignore
warn
```

---

## Cancellation

When the LangGraph task is cancelled:

- Cancellation is propagated into ACP.
- The active turn stops.
- The reusable ACP session is not destroyed unless requested.

---

## Session close

When explicitly requested:

- ACP session close is invoked where supported.
- The session binding can be deleted from `ACPSessionStore`.
- Session cleanup is distinct from prompt cancellation.

---

## Errors

ACP failures become typed errors containing useful context such as:

```text
agent
thread
session key
ACP session ID
node
operation
```

---

## Secrets

Secret values must never appear in:

- Checkpointed state.
- Event payloads.
- Results.
- Exception messages intended for normal logs.

---

## Forward compatibility

Unknown ACP updates are exposed as:

```text
acp.raw
```

rather than causing execution failure.

---

# Recommended Ticket Breakdown

The implementation should be split so each ticket introduces a usable layer without requiring the entire system to exist.

## Ticket 1 — Scaffold `langgraph-acp` and Core Types

Create the package and establish the basic domain types.

Implement:

```text
ACPResult
ACPEvent
ACPUsage
ACPSession
ACPSessionRef
ACPSessionBinding
ACPWorkspace
ACPConfig
ACPRequirements
ACPError hierarchy
```

Define public exports.

Do not connect to ACP yet.

### Acceptance

- Package imports cleanly.
- Core types are typed and documented.
- Unit tests establish equality/serialization behavior where appropriate.
- No dependency on a specific ACP agent implementation.

---

## Ticket 2 — ACP Agent Provider and Registry

Introduce the provider abstraction.

Implement:

```text
ACPAgentProvider
ACPAgentRegistry
ACPConnection abstraction
```

Provide one initial provider, likely Codex.

### Acceptance

```python
registry.resolve("codex")
```

returns a valid provider.

A provider can:

- Launch/connect.
- Perform ACP initialization.
- Return advertised capabilities.
- Shut down cleanly.

No LangGraph integration yet.

---

## Ticket 3 — Session Store

Implement the typed session-binding layer.

Implement:

```text
ACPSessionStore
InMemoryACPSessionStore
```

Optionally add a LangGraph-backed implementation if convenient.

### Acceptance

The following lifecycle works:

```python
await store.put(thread, key, session_id)
await store.get(thread, key)
await store.delete(thread, key)
```

Multiple session keys can coexist under one LangGraph thread.

Document clearly that this store contains only ACP session identifiers, not agent memory.

---

## Ticket 4 — Minimal `ACPNode`

Implement the first working LangGraph node.

Support:

```python
ACPNode(
    agent="codex",
)
```

Behavior:

- Resolve provider.
- Connect.
- Initialize ACP.
- Create session.
- Send prompt.
- Return final result.

### Acceptance

A LangGraph graph can contain:

```python
graph.add_node(
    "agent",
    ACPNode(agent="codex"),
)
```

and receive an `ACPResult`.

Streaming, permissions, MCP, and resume are not required yet.

This is the first meaningful end-to-end milestone.

---

## Ticket 5 — Session Reuse and Resume

Integrate `ACPSession` and `ACPSessionStore`.

Implement:

```text
session_key
new
reuse
resume
```

### Acceptance

First invocation:

```text
no binding
→ session/new
→ persist session ID
```

Second invocation:

```text
binding exists
→ session/resume
→ session/prompt
```

The same ACP conversation is restored by the agent runtime.

Multiple logical agents in one LangGraph thread remain isolated.

---

## Ticket 6 — ACP Event Streaming

Normalize ACP session updates into `ACPEvent`.

Support at minimum:

```text
message
thought
tool
plan
session
prompt completion
error
raw
```

Bridge them into LangGraph's custom event/streaming mechanism.

### Acceptance

A consumer can see tool execution and message progress before the node completes.

Unknown ACP updates appear as `acp.raw`.

---

## Ticket 7 — Workspace and Runtime Resolvers

Introduce the general pattern whereby node configuration can be either:

```text
static
```

or:

```python
lambda state, context: ...
```

Apply it first to:

```text
prompt
session key
workspace
```

### Acceptance

The same `ACPNode` instance can run against two LangGraph states and resolve different:

```text
cwd
additional directories
session keys
prompts
```

without mutation or reconstruction of the node.

---

## Ticket 8 — Runtime MCP Injection

Implement:

```text
MCPServer
MCPServer.stdio
MCPServer.http
```

and:

```python
ACPNode(
    mcp_servers=...
)
```

Support dynamic MCP resolution from state/context.

### Acceptance

A workflow can expose an MCP server to one ACP invocation but not another.

The ACP agent can call a tool supplied by that runtime MCP server.

Capability incompatibilities produce typed errors.

---

## Ticket 9 — Permission Policy

Implement:

```text
PermissionPolicy
PermissionRule
allow
deny
ask
```

Support both declarative rules and custom policy functions.

### Acceptance

ACP permission requests can be:

- Automatically approved.
- Automatically denied.
- Escalated to interaction handling.

No external interaction mechanism is required beyond an in-process implementation yet.

---

## Ticket 10 — Live Interaction Broker

Implement:

```text
ACPInteractionBroker
InProcessInteractionBroker
```

The broker must support a request remaining pending while the ACP prompt remains active.

Integrate ACP permission requests.

### Acceptance

A prompt can:

```text
request permission
→ wait
→ receive approval
→ continue
```

without:

- Restarting `ACPNode`.
- Sending a second ACP prompt.
- Creating a new ACP session.

This ticket proves the core live-approval architecture.

---

## Ticket 11 — External Approval Resolution

Add a broker implementation suitable for the workflow engine/web UI.

The exact transport can remain outside this package, but the broker should support:

```text
request ID
pending request registry
resolve(request_id, response)
```

### Acceptance

One task can await:

```text
approval_123
```

while another process/request resolves:

```python
resolve("approval_123", APPROVE)
```

and the original ACP turn continues.

---

## Ticket 12 — Elicitation

Extend `ACPInteractionBroker` to support ACP elicitation.

Implement:

```text
request_elicitation
elicitation events
elicitation responses
```

### Acceptance

An ACP agent can ask a structured question, receive an externally supplied answer, and continue the same prompt.

---

## Ticket 13 — ACP Config and Capability Negotiation

Implement:

```text
ACPConfig
ACPRequirements
```

Read advertised ACP capabilities and config options.

Apply requested settings where supported.

### Acceptance

A node can request semantic settings such as:

```text
model
mode
thought level
```

Unsupported required capabilities fail before prompt execution with `ACPAgentCapabilityError`.

---

## Ticket 14 — Usage

Normalize ACP usage information.

Support:

```text
turn usage
session usage
context usage
cost where provided
```

### Acceptance

Usage is available through:

```text
ACPEvent
ACPResult
```

without requiring consumers to understand provider-specific usage formats.

---

## Ticket 15 — Cancellation and Session Close

Implement lifecycle cleanup.

Support:

```text
cancel current prompt
close ACP session
remove binding
```

Ensure cancellation and closure remain separate operations.

### Acceptance

Cancelling a LangGraph task cancels the ACP turn.

Resuming the same logical agent afterward still works unless the session was explicitly closed.

---

## Ticket 16 — Secret Handling

Introduce:

```text
SecretRef
secret resolver interface
```

Use it for:

```text
ACP provider auth
MCP environment variables
runtime credentials
```

### Acceptance

Secret material is absent from:

```text
ACPEvent
ACPResult
LangGraph state
session bindings
normal logs
```

---

## Ticket 17 — Production Session Store Implementations

Once the semantics are proven, add durable implementations as required:

```text
LangGraphACPSessionStore
SqliteACPSessionStore
PostgresACPSessionStore
```

Do not block initial development on these.

### Acceptance

Two independent application processes can resolve the same logical session binding against the durable implementation.

---

## Ticket 18 — Provider Compatibility Suite

Build contract tests that can be run against each registered ACP agent.

The suite should verify:

```text
initialize
session/new
session/resume
prompt
streaming
MCP where supported
permission requests where supported
elicitation where supported
usage where supported
cancel
close
```

### Acceptance

Adding another ACP provider requires satisfying the same compatibility contract rather than introducing special behavior into `ACPNode`.

---

# Recommended Build Order

The shortest path to proving the architecture is:

```text
1  Core types
2  Agent provider + registry
3  Session store
4  Minimal ACPNode
5  Resume
6  Streaming
7  Runtime resolvers
8  MCP injection
9  Permission policy
10 Live interaction broker
11 External approval resolution
12 Elicitation
13 Config/capabilities
14 Usage
15 Cancellation/close
16 Secrets
17 Durable stores
18 Provider compatibility suite
```

The most important milestone is after Ticket 5:

```text
LangGraph
    ↓
ACPNode
    ↓
Codex
    ↓
persistent ACP conversation across graph invocations
```

The next major architectural milestone is Ticket 10:

```text
running ACPNode
    ↓
agent requests approval
    ↓
external decision
    ↓
same ACP turn continues
```

And the third is Ticket 8:

```text
workflow state
    ↓
runtime MCP selection
    ↓
ACP session
    ↓
task-specific agent capabilities
```

Once those three behaviors work, the central architecture is proven. Everything afterward is mostly hardening, compatibility, and ergonomics.

# Final Architectural Summary

`langgraph-acp` should remain a narrow bridge:

```text
                    LangGraph
                        │
                        ▼
                     ACPNode
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
   ACPSessionStore   Policies      ACPEvents
          │             │
          │      InteractionBroker
          │             │
          └──────┬──────┘
                 ▼
             ACP Client
                 │
                 ▼
       ACP-compatible agent
                 │
          ┌──────┼──────┐
          ▼      ▼      ▼
        MCP    native   agent
       tools    tools   memory
```

The package should not become an agent framework of its own.

LangGraph remains responsible for workflow orchestration.

The ACP implementation remains responsible for agent execution and conversation state.

`langgraph-acp` is responsible for making those two systems compose cleanly, durably, and interactively.
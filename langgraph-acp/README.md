# langgraph-acp

Use [ACP](https://agentclientprotocol.com)-compatible agents — Codex, Claude,
and anything else that speaks the protocol — as first-class LangGraph nodes.

```text
LangGraph owns orchestration and workflow durability.
ACP owns the agent conversation and agent-side session state.
langgraph-acp owns the binding between the two.
```

The design and the ticket sequence live in
[docs/langgraph-acp Architecture and Implementation Plan.md](../docs/langgraph-acp%20Architecture%20and%20Implementation%20Plan.md).

## Status

Tickets 1 to 3 of that plan: the core types, a connection to a real agent, and
the store that remembers which conversation belongs to which node. `ACPNode`
arrives later, so nothing here is a LangGraph node yet — but an ACP agent can be
launched, initialized, prompted, and streamed from today.

```python
from langgraph_acp import default_registry

client = await default_registry().resolve("codex").connect()
session = await client.new_session(cwd="/path/to/checkout")

async for event in session.prompt("Review this change"):
    print(event.name, event.data)

await client.close()
```

Available now:

| | |
| --- | --- |
| `ACPAgentProvider`, `ACPAgentRegistry`, `default_registry` | names resolve to agents |
| `StdioACPProvider`, `CodexACPProvider` | how an agent is launched |
| `ACPClient`, `ACPCapabilities` | one live connection, and what it can do |
| `ACPSession` | one conversation, and the turns run in it |
| `ACPSessionSpec`, `ACPSessionStrategy` | which conversation a node speaks in |
| `ACPSessionStore`, `InMemoryACPSessionStore` | which ACP session a node's identity resolves to |
| `ACPWorkspace` | the filesystem context a session is given |
| `ACPConfig`, `ACPRequirements` | settings requested, capabilities demanded |
| `ACPEvent`, `ACPEventType` | streamed activity, normalized |
| `ACPResult`, `ACPUsage` | what a turn returns, and what it cost |
| `ACPError` and its subclasses | typed failures |

Registering another ACP agent is a command line rather than a class:

```python
default_registry().register(
    StdioACPProvider(name="gemini", command=["gemini", "--experimental-acp"])
)
```

## Session bindings

`ACPSessionStore` maps a node's logical identity to the ACP session it speaks
in, so that a workflow can pick a conversation back up later:

```python
store = InMemoryACPSessionStore()

await store.put("pr-918", "reviewer", "sess_abc123")
await store.get("pr-918", "reviewer")     # -> "sess_abc123"
await store.delete("pr-918", "reviewer")
```

The identity is a pair, not a thread id, because one LangGraph thread routinely
runs several agents — an implementer and three reviewers on the same pull
request — and none of them may resume another's conversation.

**A store holds ACP session identifiers and nothing else.** No conversation
history, no agent messages, no tool history, no model context, no agent memory:
the ACP agent owns all of that and restores it itself when asked to load
`sess_abc123`. That is what makes a reply arriving on a webhook days later
resume a live conversation without reconstructing a transcript — and why the
store is the piece that has to be durable. `InMemoryACPSessionStore` is not; it
lives as long as its process, which is right for tests, examples, and
single-process graphs. The durable implementations arrive with Ticket 17 and
change nothing about the interface.

Two behaviours are placeholders that later tickets replace. Permission requests
are streamed as `acp.permission.requested` and then declined, because the policy
that could approve them does not exist yet; and the history an agent replays
while loading a session is dropped, because no turn is streaming when it
arrives.

## Development

A standalone distribution, deliberately outside the `uv` workspace at the root
of this repository: it depends on nothing OpenEngine ships, targets a newer
Python than the rest of the tree, and is intended to be published on its own.

```bash
cd langgraph-acp
uv run pytest
uv run mypy    # strict, over src/ and tests/
```

The package ships `py.typed`, so its annotations are a promise to a downstream
type checker; `mypy --strict` runs in CI on both supported interpreters to keep
it. Each field is annotated with what the constructor accepts, since
`__post_init__` normalizes — a `Path` becomes a `str`, `"reuse"` becomes
`ACPSessionStrategy.REUSE`.

The dependency list is empty and stays empty: ACP is JSON-RPC over a pipe, so
the client is stdlib `asyncio` written here rather than a second protocol
library in every application that installs this one. The client tests launch a
real child process — `tests/fake_agent.py`, an ACP agent that does nothing,
correctly — because the thing under test is a process boundary.

CI runs both only when something under `langgraph-acp/` changes.

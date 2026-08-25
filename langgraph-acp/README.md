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

Ticket 1 of that plan: the core types only. `ACPNode`, the agent registry, and
the session store arrive in later tickets, so nothing here connects to an agent
yet.

```python
from langgraph_acp import ACPResult, ACPSession, ACPSessionBinding, ACPUsage
```

Available now:

| | |
| --- | --- |
| `ACPSession`, `ACPSessionRef`, `ACPSessionBinding` | which conversation a node speaks in |
| `ACPWorkspace` | the filesystem context a session is given |
| `ACPConfig`, `ACPRequirements` | settings requested, capabilities demanded |
| `ACPEvent`, `ACPEventType` | streamed activity, normalized |
| `ACPResult`, `ACPUsage` | what a turn returns, and what it cost |
| `ACPError`, `ACPAgentCapabilityError`, `ACPSessionError` | typed failures |

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

CI runs both only when something under `langgraph-acp/` changes.

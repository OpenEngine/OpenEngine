"""What each node's agent consumed, in approximate dollars, and a WorkOrder's sum.

An agent node publishes `usage.updated` as its conversation reports usage: the
tokens of every prompt turn, and -- when the agent says -- what the session has
cost so far. Nothing else is stored. The totals are a reduction over the run's
event log, so they cannot drift from it -- and last only as long as it does,
which for the web app's in-memory log is until the server restarts.

    usage.updated (per turn, per session)
        -> SessionUsage   reported session cost, else priced tokens
        -> node total     sum of that node's sessions
        -> WorkOrder      sum of every node (a WorkOrder id is its run id)

Dollars are approximate. A cost the agent reports is used as-is; otherwise the
tokens are priced from `PRICES`, list rates that know nothing of discounts,
plans or billing. An agent that reported nothing is unknown rather than free:
its cost is `None`, and any total it contributes to says it is incomplete.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from engine.graph_runtime.events import EventKind, RuntimeEvent


@dataclass(frozen=True, slots=True)
class TokenPrice:
    """List price in USD per million tokens."""

    input: float
    output: float
    cache_read: float
    cache_write: float

    def cost(self, usage: TokenCounts) -> float:
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cached_read_tokens * self.cache_read
            + usage.cached_write_tokens * self.cache_write
        ) / 1_000_000


_OPUS = TokenPrice(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25)
_CODEX = TokenPrice(input=1.25, output=10.0, cache_read=0.125, cache_write=1.25)

#: Matched in order against the model a node configured, then its agent name.
#: The agent's own name is the fallback because the model is usually left to
#: the adapter: `claude` is priced as Opus and `codex` as GPT-5 Codex.
PRICES: tuple[tuple[str, TokenPrice], ...] = (
    ("fable", TokenPrice(input=10.0, output=50.0, cache_read=1.0, cache_write=12.5)),
    ("opus", _OPUS),
    ("sonnet", TokenPrice(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75)),
    ("haiku", TokenPrice(input=1.0, output=5.0, cache_read=0.1, cache_write=1.25)),
    ("codex", _CODEX),
    ("gpt", _CODEX),
    ("claude", _OPUS),
)


def price_for(agent: str, model: str = "") -> TokenPrice | None:
    """The rate for this agent and model, or `None` for one nobody priced."""
    for name in (model.lower(), agent.lower()):
        for fragment, price in PRICES:
            if name and fragment in name:
                return price
    return None


def _amount(value: object) -> float | None:
    """`value` as a finite, non-negative number, or `None` for anything else.

    JSON can spell infinity (`1e400`), which no response can serialize back.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    amount = float(value)
    return amount if math.isfinite(amount) and amount >= 0 else None


@dataclass(frozen=True, slots=True)
class TokenCounts:
    """Tokens summed over disjoint prompt turns.

    `input_tokens` excludes the cache: ACP reports fresh input, cache reads and
    cache writes separately because each is billed at its own rate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0

    def __add__(self, other: TokenCounts) -> TokenCounts:
        return TokenCounts(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cached_read_tokens + other.cached_read_tokens,
            self.cached_write_tokens + other.cached_write_tokens,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> TokenCounts:
        def count(name: str) -> int:
            value = _amount(payload.get(name))
            return int(value) if value is not None else 0

        return cls(
            count("inputTokens"),
            count("outputTokens"),
            count("cachedReadTokens"),
            count("cachedWriteTokens"),
        )


@dataclass(frozen=True, slots=True)
class Usage:
    """What something consumed, and its approximate cost in USD."""

    tokens: TokenCounts = TokenCounts()
    cost_usd: float | None = None
    """`None` when nothing contributing to it could be costed."""
    estimated: bool = False
    """Whether any of `cost_usd` was priced from tokens rather than reported."""
    complete: bool = True
    """Whether every contributing session could be costed."""

    def __add__(self, other: Usage) -> Usage:
        costs = [cost for cost in (self.cost_usd, other.cost_usd) if cost is not None]
        return Usage(
            tokens=self.tokens + other.tokens,
            cost_usd=sum(costs) if costs else None,
            estimated=self.estimated or other.estimated,
            complete=self.complete and other.complete,
        )

    def json(self) -> dict[str, object]:
        return {
            "costUsd": None if self.cost_usd is None else round(self.cost_usd, 6),
            "estimated": self.estimated,
            "complete": self.complete,
            "inputTokens": self.tokens.input_tokens,
            "outputTokens": self.tokens.output_tokens,
            "cachedReadTokens": self.tokens.cached_read_tokens,
            "cachedWriteTokens": self.tokens.cached_write_tokens,
        }


@dataclass(slots=True)
class _Session:
    agent: str = ""
    model: str = ""
    tokens: TokenCounts = TokenCounts()
    turns: int = 0
    reported_cost: float | None = None
    """The agent's own running total for the session; the latest one wins."""

    def usage(self) -> Usage:
        if self.reported_cost is not None:
            return Usage(self.tokens, self.reported_cost)
        price = price_for(self.agent, self.model)
        if price is None or not self.turns:
            return Usage(self.tokens, None, complete=False)
        return Usage(self.tokens, price.cost(self.tokens), estimated=True)


@dataclass(frozen=True, slots=True)
class WorkOrderUsage:
    """Every node's usage in one run, and their total."""

    nodes: Mapping[str, Usage] = field(default_factory=dict)

    @property
    def total(self) -> Usage:
        total = Usage()
        for usage in self.nodes.values():
            total += usage
        return total

    def json(self) -> dict[str, object]:
        return {
            **self.total.json(),
            "nodes": {node: usage.json() for node, usage in self.nodes.items()},
        }


def usage_rollup(events: Iterable[RuntimeEvent]) -> WorkOrderUsage:
    """Reduce a run's `usage.updated` events to node and WorkOrder totals.

    A session is `(node, sessionId)`: a reported cost is the agent's running
    total for its session, so it replaces the last one rather than adding to
    it, and a node that opened a second conversation has two to add together.
    """
    sessions: dict[tuple[str, str], _Session] = {}
    for event in events:
        if event.kind is not EventKind.USAGE_UPDATED or event.node_id is None:
            continue
        payload = event.payload
        key = (str(event.node_id), str(payload.get("sessionId", "")))
        session = sessions.setdefault(key, _Session())
        session.agent = str(payload.get("agent") or session.agent)
        session.model = str(payload.get("model") or session.model)
        cost = _amount(payload.get("sessionCostUsd"))
        if cost is not None:
            session.reported_cost = cost
        if payload.get("turn"):
            session.tokens += TokenCounts.from_payload(payload)
            session.turns += 1
    nodes: dict[str, Usage] = {}
    for (node, _), session in sessions.items():
        nodes[node] = nodes.get(node, Usage()) + session.usage()
    return WorkOrderUsage(nodes)


__all__ = [
    "PRICES",
    "TokenCounts",
    "TokenPrice",
    "Usage",
    "WorkOrderUsage",
    "price_for",
    "usage_rollup",
]

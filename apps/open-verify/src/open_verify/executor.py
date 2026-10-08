"""Replaceable reasoning over detached runtime context, with no application access."""

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Protocol

from open_verify.models import Decision
from open_verify.prompts import CHANGE_INSTRUCTIONS, INSPECTION_INSTRUCTIONS, INSTRUCTIONS


class DecisionAgent(Protocol):
    """Provider transport, independent of runner and engine implementations."""

    async def decide(self, prompt: str) -> Decision: ...


@dataclass(frozen=True)
class DecisionContext:
    """One detached view of a phase; executors never receive live runner state."""

    payload: dict
    scope: str
    change_mode: bool

    @classmethod
    def snapshot(cls, payload: dict, *, scope: str, case_id: str | None, change_mode: bool):
        """Copy before scoping so neither built-in nor custom executors can edit the plan."""
        value = deepcopy(payload)
        state = value["state"]
        state.pop("decision", None)
        if case_id is not None:
            state["plan"]["cases"] = [c for c in state["plan"]["cases"] if c["id"] == case_id]
            state["findings"] = []
        return cls(value, scope, change_mode)


class StepExecutor(Protocol):
    """Choose one proposed decision; only the runner may apply it."""

    async def decide(self, context: DecisionContext) -> Decision: ...


class AgentExecutor:
    """The default executor: bounded prompts and separate sessions for each case."""

    def __init__(self, agent: DecisionAgent):
        self.agent = agent
        self.scope = "discover"

    async def decide(self, context: DecisionContext) -> Decision:
        """Reset conversation at a scope boundary while retaining the provider process."""
        if context.scope != self.scope:
            reset = getattr(self.agent, "reset_session", None)
            if reset is not None:
                await reset()
            self.scope = context.scope
        return await self.agent.decide(build_prompt(context))


def compact(value, limit=2500):
    """Bound historical evidence; a fresh observation gets its full tool read limit."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + " [truncated; use a targeted source section if needed]"
    if isinstance(value, dict):
        return {key: compact(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [compact(item, limit) for item in value[:60]]
    return value


def build_prompt(context: DecisionContext) -> str:
    """Render the default policy and context without truncating tool or decision schemas."""
    payload = deepcopy(context.payload)
    payload["state"] = compact(payload["state"])
    payload["state"]["observation"] = compact(context.payload["state"].get("observation"), 24000)
    payload["recent_evidence"] = compact(payload["recent_evidence"])
    payload["managed_processes"] = compact(payload["managed_processes"])
    if "change" in payload and "diff" in payload["change"]:
        patch = payload["change"]["diff"]
        payload["change"]["diff"] = patch[:40000]
        payload["change"]["truncated"] |= len(patch) > 40000
    instructions = INSTRUCTIONS + INSPECTION_INSTRUCTIONS + (CHANGE_INSTRUCTIONS if context.change_mode else "")
    return instructions + "\n" + json.dumps(payload)

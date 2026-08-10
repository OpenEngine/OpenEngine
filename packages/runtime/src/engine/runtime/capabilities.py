"""The capability set a runtime is wired with.

Holds one implementation per port. Constructed by `apps/` -- the composition
root -- and passed down. Nothing here imports a concrete adapter; the fields are
typed against `engine.ports` protocols, so any implementation of the right shape
slots in, real or fake.
"""

from dataclasses import dataclass

from engine.ports import (
    AgentRunner,
    Communications,
    SourceControl,
    StateStore,
    WorkflowRuntime,
    WorkspaceProvider,
)


@dataclass(frozen=True, slots=True)
class Capabilities:
    """One implementation per capability, resolved at composition time."""

    workflow_runtime: WorkflowRuntime
    source_control: SourceControl
    agent_runner: AgentRunner
    communications: Communications
    workspace_provider: WorkspaceProvider
    state_store: StateStore


__all__ = ["Capabilities"]

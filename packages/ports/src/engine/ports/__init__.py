"""The six capabilities the engine needs from the outside world.

Each port is a `Protocol` -- structural, so an adapter satisfies it by shape
alone with no import of this package required at runtime and no base class to
inherit. Ports name *what* is needed (publish changes, run an agent), never
*who* provides it (GitHub, Codex).

Every command in `engine.domain.commands` is fulfilled by exactly one of these.
"""

from engine.ports.agent_runner import AgentRunner, AttemptResult
from engine.ports.communications import Communications
from engine.ports.source_control import SourceControl
from engine.ports.state_store import StateStore
from engine.ports.workflow_runtime import WorkflowRuntime
from engine.ports.workspace_provider import Workspace, WorkspaceProvider

__all__ = [
    "AgentRunner",
    "AttemptResult",
    "Communications",
    "SourceControl",
    "StateStore",
    "Workspace",
    "WorkflowRuntime",
    "WorkspaceProvider",
]

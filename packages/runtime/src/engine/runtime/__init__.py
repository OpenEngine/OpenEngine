"""The runtime: binds the pure engine to concrete capabilities.

Depends on `engine.ports` (and through it `engine.domain`), never on a specific
adapter. Adapters depend on the runtime, not the other way around.
"""

from engine.runtime.capabilities import Capabilities
from engine.runtime.dispatcher import (
    Dispatcher,
    UnhandledCommandError,
    run_agent_to_completion,
)
from engine.runtime.filesystem import Workspace, WorkspaceEscape
from engine.runtime.foreman import (
    PLANNER_SYSTEM_PROMPT,
    WORKER_SYSTEM_PROMPT,
    Foreman,
    ForemanError,
    ForemanEvent,
    PlannerText,
    PlannerThinking,
    PlanUpdated,
    ToolActivity,
    TurnEnded,
    WorkerText,
    render_plan,
)
from engine.runtime.registry import (
    AGENT_RUNNER_GROUP,
    RunnerUnavailable,
    UnknownRunner,
    available_agent_runners,
    load_agent_runner,
    resolve_agent_runner,
)
from engine.runtime.tools import PLANNER_TOOLS, WORKER_TOOLS

__all__ = [
    "AGENT_RUNNER_GROUP",
    "PLANNER_SYSTEM_PROMPT",
    "PLANNER_TOOLS",
    "WORKER_SYSTEM_PROMPT",
    "WORKER_TOOLS",
    "Capabilities",
    "Dispatcher",
    "Foreman",
    "ForemanError",
    "ForemanEvent",
    "PlanUpdated",
    "PlannerText",
    "PlannerThinking",
    "RunnerUnavailable",
    "ToolActivity",
    "TurnEnded",
    "UnhandledCommandError",
    "UnknownRunner",
    "WorkerText",
    "Workspace",
    "WorkspaceEscape",
    "available_agent_runners",
    "load_agent_runner",
    "render_plan",
    "resolve_agent_runner",
    "run_agent_to_completion",
]

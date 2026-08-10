"""The tool surfaces that distinguish a planner from a worker.

This module is the whole planner/worker distinction, written out. Both run
through `engine.ports.AgentRunner`; the planner gets tools that *decompose and
delegate*, the worker gets tools that *do the work*. Neither list mentions a
vendor, so the same catalogue drives Anthropic, OpenAI, or Strands.

Note what the worker does **not** get: `dispatch_task`. A worker cannot spawn
another worker, so the delegation graph is one level deep by construction rather
than by instruction.

Descriptions are written to be prescriptive about *when* to call a tool, not just
what it does -- that is what actually moves tool-selection behaviour.
"""

from engine.ports.agent_runner import ToolSpec

SET_GOAL = ToolSpec(
    name="set_goal",
    description=(
        "Record what this piece of work is ultimately trying to achieve. "
        "Call this once, first, before adding any tasks -- the goal is what every "
        "task is judged against, and workers never see the conversation, only the "
        "plan. Re-call it only if the user changes what they want."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "One or two sentences stating the outcome, not the method.",
            }
        },
        "required": ["goal"],
        "additionalProperties": False,
    },
)

ADD_TASK = ToolSpec(
    name="add_task",
    description=(
        "Add one unit of work to the plan. Call this for each piece of the job "
        "that could be handed to someone else and finished without further "
        "conversation. Add every task you can see before dispatching any of them, "
        "so the dependency graph is complete. A task that cannot be described "
        "well enough for a stranger to execute is too big -- split it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Short stable slug, e.g. 'parse-config'. Used to express dependencies.",
            },
            "title": {
                "type": "string",
                "description": "One line, imperative: 'Add retry handling to the HTTP client'.",
            },
            "detail": {
                "type": "string",
                "description": (
                    "The complete brief for whoever executes this. Include file paths, "
                    "constraints, and what done looks like. The worker sees only this."
                ),
            },
            "depends_on": {
                "type": "array",
                "items": {"type": "string"},
                "description": "task_ids that must finish successfully before this one can start.",
            },
        },
        "required": ["task_id", "title"],
        "additionalProperties": False,
    },
)

DISPATCH_TASK = ToolSpec(
    name="dispatch_task",
    description=(
        "Hand a task to a worker and start it immediately. This returns as soon as "
        "the worker starts -- it does not wait for the result. Dispatch every task "
        "that is ready in the same turn so they run in parallel, then call "
        "await_tasks once to collect them. Do not dispatch a task whose "
        "dependencies have not finished; it will be refused."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task to execute."},
            "instructions": {
                "type": "string",
                "description": (
                    "Optional override for the brief. Omit to use the task's detail. "
                    "Supply it when this worker needs context the task itself doesn't carry."
                ),
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

AWAIT_TASKS = ToolSpec(
    name="await_tasks",
    description=(
        "Wait for dispatched workers to finish and return what each one did. Call "
        "this after dispatching, when you need the results to decide what happens "
        "next. Omit task_ids to wait for everything currently running."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific tasks to wait for. Omit to wait for all running tasks.",
            }
        },
        "additionalProperties": False,
    },
)

LIST_TASKS = ToolSpec(
    name="list_tasks",
    description=(
        "Return the current plan with every task's status and result. Call this "
        "when you have lost track of what is outstanding, or before telling the "
        "user where things stand -- report the plan's actual state, not your "
        "memory of it."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
)

#: What makes an agent a planner. Everything here is about deciding and
#: delegating; none of it touches a file.
PLANNER_TOOLS: tuple[ToolSpec, ...] = (
    SET_GOAL,
    ADD_TASK,
    DISPATCH_TASK,
    AWAIT_TASKS,
    LIST_TASKS,
)


LIST_FILES = ToolSpec(
    name="list_files",
    description=(
        "List files in the workspace matching a glob. Call this first to orient "
        "yourself before reading or writing anything."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob relative to the workspace root, e.g. 'src/**/*.py'. Defaults to '*'.",
            }
        },
        "additionalProperties": False,
    },
)

READ_FILE = ToolSpec(
    name="read_file",
    description=(
        "Read a file from the workspace. Always read a file before editing it -- "
        "write_file replaces the whole file, so writing blind loses content."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the workspace root."}
        },
        "required": ["path"],
        "additionalProperties": False,
    },
)

WRITE_FILE = ToolSpec(
    name="write_file",
    description=(
        "Write a file in the workspace, replacing it entirely and creating parent "
        "directories as needed. Pass the complete intended contents, not a diff or "
        "a fragment."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "content": {"type": "string", "description": "The complete file contents."},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
)

REPORT = ToolSpec(
    name="report",
    description=(
        "Report the outcome of your task. Call this exactly once, last, when the "
        "work is done or you are blocked. State plainly what you changed and what "
        "you did not; if you could not finish, say what stopped you."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "succeeded": {"type": "boolean", "description": "Whether the task was completed."},
            "summary": {
                "type": "string",
                "description": "What you did, in a few sentences. This is all the planner sees.",
            },
        },
        "required": ["succeeded", "summary"],
        "additionalProperties": False,
    },
)

#: What makes an agent a worker. Note the absence of dispatch_task.
#:
#: There is deliberately no shell tool here. A model-authored `run_command`
#: needs an executable allowlist, argument rejection, timeouts, and an isolated
#: filesystem to be safe, and the workspace provider that would supply the last
#: of those is not implemented yet. Filesystem access below is confined to the
#: workspace root; see `engine.runtime.filesystem`.
WORKER_TOOLS: tuple[ToolSpec, ...] = (LIST_FILES, READ_FILE, WRITE_FILE, REPORT)


__all__ = [
    "ADD_TASK",
    "AWAIT_TASKS",
    "DISPATCH_TASK",
    "LIST_FILES",
    "LIST_TASKS",
    "PLANNER_TOOLS",
    "READ_FILE",
    "REPORT",
    "SET_GOAL",
    "WORKER_TOOLS",
    "WRITE_FILE",
]

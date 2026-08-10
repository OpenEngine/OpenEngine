"""Agent Runner capability backed by a deterministic script.

Not a mock -- a real implementation of the port whose "model" is a lookup table.
It runs the same loop shape as a live backend (emit text, call tools, read the
results, continue), so the Foreman, the tool surface, and the web UI are all
exercised end to end with no network and no credentials.

That makes it the offline demo *and* the test double, which is the point: if the
scripted runner and the Claude runner can both drive the planner unchanged, the
port is genuinely provider-neutral.

Scripts are keyed by agent role -- 'planner' or 'worker' -- and replayed turn by
turn. `DEMO_SCRIPT` plans and executes a small real job against the workspace.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import count

from engine.ports.agent_runner import (
    AgentEvent,
    AgentSpec,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    ToolInvoker,
    TurnFinished,
)


@dataclass(frozen=True, slots=True)
class ScriptedToolCall:
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    """One scripted response: some text, then some tool calls."""

    text: str = ""
    tool_calls: tuple[ScriptedToolCall, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Script:
    """The turns an agent of a given role will produce, in order."""

    turns: tuple[ScriptedTurn, ...]
    #: Repeat the final turn once the script runs out, instead of going silent.
    repeat_last: bool = True


def _role_of(spec: AgentSpec) -> str:
    return "planner" if spec.agent_id.startswith("planner") else "worker"


class ScriptedAgentSession:
    """Replays a script. Implements `engine.ports.AgentSession`."""

    def __init__(self, spec: AgentSpec, invoke_tool: ToolInvoker, script: Script) -> None:
        self._spec = spec
        self._invoke_tool = invoke_tool
        self._script = script
        self._turn = 0
        self._call_ids = count(1)
        self._closed = False

    def send(self, message: str) -> AsyncIterator[AgentEvent]:
        return self._run(message)

    async def _run(self, message: str) -> AsyncIterator[AgentEvent]:
        turn = self._next_turn()
        if turn is None:
            yield TurnFinished("end_turn")
            return

        if turn.text:
            # Chunked so consumers exercise their streaming path.
            for word in turn.text.split(" "):
                yield TextDelta(word + " ")

        for call in turn.tool_calls:
            call_id = f"call-{next(self._call_ids)}"
            yield ToolCallStarted(call_id, call.name, call.arguments)
            result = await self._invoke_tool(call.name, call.arguments)
            yield ToolCallFinished(call_id, call.name, result)

        yield TurnFinished("end_turn")

    def _next_turn(self) -> ScriptedTurn | None:
        turns = self._script.turns
        if not turns:
            return None
        if self._turn < len(turns):
            turn = turns[self._turn]
            self._turn += 1
            return turn
        return turns[-1] if self._script.repeat_last else None

    async def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class ScriptedAgentRunner:
    """Implements `engine.ports.AgentRunner` from a table of scripts."""

    def __init__(self, scripts: Mapping[str, Script]) -> None:
        self._scripts = dict(scripts)
        self.started: list[AgentSpec] = []

    def start(self, spec: AgentSpec, invoke_tool: ToolInvoker) -> ScriptedAgentSession:
        self.started.append(spec)
        # Exact agent id first, so a script can differ per task, then the role.
        script = self._scripts.get(spec.agent_id) or self._scripts.get(
            _role_of(spec), Script(turns=())
        )
        return ScriptedAgentSession(spec, invoke_tool, script)

    def specs_for(self, role: str) -> Sequence[AgentSpec]:
        """Every spec started for a role -- useful for asserting tool surfaces."""
        return [s for s in self.started if _role_of(s) == role]


#: A self-contained demo: plan a two-task job, run both, then report.
#: The worker actually writes files, so the UI shows real work happening.
DEMO_SCRIPT: dict[str, Script] = {
    "planner": Script(
        turns=(
            ScriptedTurn(
                text="Right — let me break that down and get people on it.",
                tool_calls=(
                    ScriptedToolCall(
                        "set_goal",
                        {"goal": "Produce a short project brief and a matching README."},
                    ),
                    ScriptedToolCall(
                        "add_task",
                        {
                            "task_id": "brief",
                            "title": "Write the project brief",
                            "detail": (
                                "Create BRIEF.md at the workspace root. Two short "
                                "sections: 'Problem' and 'Approach'. Keep it under "
                                "200 words. Then call report."
                            ),
                        },
                    ),
                    ScriptedToolCall(
                        "add_task",
                        {
                            "task_id": "readme",
                            "title": "Write the README",
                            "detail": (
                                "Read BRIEF.md, then create README.md summarising it "
                                "in one paragraph plus a 'Getting started' section. "
                                "Then call report."
                            ),
                            "depends_on": ["brief"],
                        },
                    ),
                    ScriptedToolCall("dispatch_task", {"task_id": "brief"}),
                    ScriptedToolCall("await_tasks", {}),
                    ScriptedToolCall("dispatch_task", {"task_id": "readme"}),
                    ScriptedToolCall("await_tasks", {}),
                    ScriptedToolCall("list_tasks", {}),
                ),
            ),
            ScriptedTurn(
                text="Both tasks are done — BRIEF.md and README.md are in the workspace.",
            ),
        )
    ),
    "worker-brief": Script(
        turns=(
            ScriptedTurn(
                text="Writing the brief.",
                tool_calls=(
                    ScriptedToolCall("list_files", {"pattern": "*"}),
                    ScriptedToolCall(
                        "write_file",
                        {
                            "path": "BRIEF.md",
                            "content": (
                                "# Project brief\n\n"
                                "## Problem\n\n"
                                "Work arrives faster than any one agent can carry it, and "
                                "a single agent holding the whole job loses the thread.\n\n"
                                "## Approach\n\n"
                                "A foreman decomposes the work into self-contained tasks "
                                "and delegates each to a worker.\n"
                            ),
                        },
                    ),
                    ScriptedToolCall(
                        "report",
                        {"succeeded": True, "summary": "Wrote BRIEF.md with both sections."},
                    ),
                ),
            ),
        )
    ),
    "worker-readme": Script(
        turns=(
            ScriptedTurn(
                text="Reading the brief, then writing the README.",
                tool_calls=(
                    ScriptedToolCall("read_file", {"path": "BRIEF.md"}),
                    ScriptedToolCall(
                        "write_file",
                        {
                            "path": "README.md",
                            "content": (
                                "# engine\n\n"
                                "Work arrives faster than one agent can carry it, so a "
                                "foreman decomposes the job into self-contained tasks and "
                                "delegates each to a worker.\n\n"
                                "## Getting started\n\n"
                                "```bash\n"
                                "uv sync\n"
                                "uv run engine-control-server\n"
                                "```\n"
                            ),
                        },
                    ),
                    ScriptedToolCall(
                        "report",
                        {"succeeded": True, "summary": "Wrote README.md from BRIEF.md."},
                    ),
                ),
            ),
        )
    ),
    # Fallback for any task the demo script doesn't name explicitly.
    "worker": Script(
        turns=(
            ScriptedTurn(
                text="Looking at the workspace.",
                tool_calls=(
                    ScriptedToolCall("list_files", {"pattern": "*"}),
                    ScriptedToolCall(
                        "report",
                        {
                            "succeeded": True,
                            "summary": "No scripted behaviour for this task; inspected the workspace only.",
                        },
                    ),
                ),
            ),
        )
    ),
}


def build_agent_runner(**options: object) -> ScriptedAgentRunner:
    """Plugin factory, registered under `engine.agent_runners` as 'scripted'.

    Always available -- no credentials, no network. That is what makes it a
    usable last entry in a preference list.
    """
    scripts = options.get("scripts")
    return ScriptedAgentRunner(scripts if isinstance(scripts, Mapping) else DEMO_SCRIPT)


__all__ = [
    "DEMO_SCRIPT",
    "Script",
    "ScriptedAgentRunner",
    "ScriptedAgentSession",
    "ScriptedToolCall",
    "ScriptedTurn",
    "build_agent_runner",
]

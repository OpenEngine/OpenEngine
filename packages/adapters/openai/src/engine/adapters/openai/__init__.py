"""Agent Runner capability, backed by OpenAI.

Placeholder. Satisfies `engine.ports.AgentRunner` structurally -- including the
tool surface, which is the part that matters: the same `PLANNER_TOOLS` that drive
the Anthropic adapter are handed here unchanged, because the port speaks JSON
Schema rather than any vendor's tool format.

Named for the provider rather than the product, like every other adapter. Which
agent a provider offers (Codex, or whatever succeeds it) is a `model` choice on
`AgentSpec`, not a separate adapter.

No process spawning, sandboxing, or output parsing yet.
"""

from collections.abc import AsyncIterator

from engine.ports.agent_runner import AgentEvent, AgentSpec, ToolInvoker

DEFAULT_MODEL = "gpt-5-codex"


class OpenAIAgentSession:
    """Implements `engine.ports.AgentSession`."""

    def __init__(self, spec: AgentSpec, invoke_tool: ToolInvoker) -> None:
        self._spec = spec
        self._invoke_tool = invoke_tool

    def send(self, message: str) -> AsyncIterator[AgentEvent]:
        raise NotImplementedError("OpenAI execution lands with the agent-runner ticket")

    async def close(self) -> None:
        raise NotImplementedError("OpenAI teardown lands with the agent-runner ticket")


class OpenAIAgentRunner:
    """Runs planners and workers on OpenAI.

    Implements `engine.ports.AgentRunner`.
    """

    def __init__(self, *, model: str = DEFAULT_MODEL, timeout_seconds: float = 3600.0) -> None:
        self._model = model
        self._timeout_seconds = timeout_seconds

    def start(self, spec: AgentSpec, invoke_tool: ToolInvoker) -> OpenAIAgentSession:
        return OpenAIAgentSession(spec, invoke_tool)


def build_agent_runner(**options: object) -> OpenAIAgentRunner:
    """Plugin factory, registered under `engine.agent_runners` as 'openai'.

    Constructs fine; every session raises NotImplementedError until the
    agent-runner ticket lands. It is registered anyway so `ENGINE_AGENT_RUNNER`
    lists it and the plugin wiring is exercised by more than one vendor.
    """
    model = options.get("model")
    return OpenAIAgentRunner(model=str(model) if model else DEFAULT_MODEL)


__all__ = [
    "DEFAULT_MODEL",
    "OpenAIAgentRunner",
    "OpenAIAgentSession",
    "build_agent_runner",
]

"""Agent Runner capability backed by the Claude API.

Implements `engine.ports.AgentRunner` with the Messages API and tool use. The
planner's tools are built at runtime from `ToolSpec` objects, so this adapter
never knows what a plan is -- it forwards JSON Schema and hands tool calls back
to whatever invoker the host supplied.

**Why a manual loop rather than the SDK tool runner.** The tool runner builds
schemas from decorated Python functions; our tools arrive as data (`ToolSpec`)
and are executed by a host-supplied callback so the host keeps the approval gate
and can turn a tool call into an engine event. That is the documented case for
owning the loop, and it is only about twenty lines.
"""

import json
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

from engine.ports.agent_runner import (
    AgentEvent,
    AgentSpec,
    TextDelta,
    Thinking,
    ToolCallFinished,
    ToolCallStarted,
    ToolInvoker,
    ToolResult,
    ToolSpec,
    TurnFinished,
)
from engine.runtime.registry import RunnerUnavailable

DEFAULT_MODEL = "claude-opus-5"

#: Streaming, so a large ceiling costs nothing until it is used. Thinking is on
#: by default on Opus 5 and counts against this, so leave real headroom.
DEFAULT_MAX_TOKENS = 32_000

#: Guard against a tool loop that never terminates.
DEFAULT_MAX_ITERATIONS = 40

#: Safety classifiers can decline a request; this re-runs it on Anthropic's
#: recommended fallback in the same call rather than surfacing the refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _tool_payload(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": dict(tool.input_schema),
    }


def credentials_available() -> bool:
    """Whether a Claude client is likely to authenticate.

    An unset ANTHROPIC_API_KEY does not mean there are no credentials -- the SDK
    also resolves an `ant auth login` profile from disk. Checking for that file
    keeps the control server from advertising a live planner it cannot start,
    without making a network call at boot.
    """
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    config_dir = os.environ.get("ANTHROPIC_CONFIG_DIR") or os.path.expanduser(
        "~/.config/anthropic"
    )
    return os.path.isdir(os.path.join(config_dir, "credentials"))


class AnthropicAgentSession:
    """One Claude conversation. Implements `engine.ports.AgentSession`."""

    def __init__(
        self,
        client: Any,
        spec: AgentSpec,
        invoke_tool: ToolInvoker,
        *,
        model: str,
        max_tokens: int,
        max_iterations: int,
    ) -> None:
        self._client = client
        self._spec = spec
        self._invoke_tool = invoke_tool
        self._model = model
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations
        self._messages: list[dict[str, Any]] = []
        self._tools = [_tool_payload(t) for t in spec.tools]

    def send(self, message: str) -> AsyncIterator[AgentEvent]:
        return self._run(message)

    async def _run(self, message: str) -> AsyncIterator[AgentEvent]:
        self._messages.append({"role": "user", "content": message})

        for _ in range(self._max_iterations):
            response = None
            async with self._client.beta.messages.stream(
                model=self._model,
                max_tokens=self._max_tokens,
                system=self._spec.system_prompt or None,
                tools=self._tools or None,
                messages=self._messages,
                # Opus 5 thinks by default and omits the text; ask for the
                # summary so the UI can show progress instead of a long pause.
                thinking={"type": "adaptive", "display": "summarized"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            ) as stream:
                async for event in stream:
                    emitted = _translate(event)
                    if emitted is not None:
                        yield emitted
                response = await stream.get_final_message()

            self._messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                category = getattr(getattr(response, "stop_details", None), "category", None)
                yield TextDelta(
                    "\n[The request was declined by safety classifiers"
                    + (f" ({category})" if category else "")
                    + ".]"
                )
                yield TurnFinished("refusal")
                return

            if response.stop_reason == "pause_turn":
                # A server-side tool hit its iteration cap. Re-send to resume;
                # the API picks up where it left off.
                continue

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                yield TurnFinished(response.stop_reason or "end_turn")
                return

            results = []
            for block in tool_uses:
                arguments: Mapping[str, object] = block.input or {}
                yield ToolCallStarted(block.id, block.name, arguments)
                result = await self._invoke_tool(block.name, arguments)
                yield ToolCallFinished(block.id, block.name, result)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.content or "(no output)",
                        "is_error": result.is_error,
                    }
                )
            # All results go back in a single user message -- splitting them
            # trains the model out of making parallel calls.
            self._messages.append({"role": "user", "content": results})

        yield TextDelta(
            f"\n[Stopped after {self._max_iterations} tool iterations without finishing.]"
        )
        yield TurnFinished("max_iterations")

    async def close(self) -> None:
        self._messages.clear()


def _translate(event: Any) -> AgentEvent | None:
    """Map an SDK stream event onto a port event, or None to ignore it."""
    if event.type == "content_block_delta":
        delta = event.delta
        if delta.type == "text_delta":
            return TextDelta(delta.text)
        if delta.type == "thinking_delta":
            return Thinking(delta.thinking)
    elif event.type == "content_block_start":
        if getattr(event.content_block, "type", None) == "thinking":
            return Thinking()
    return None


class AnthropicAgentRunner:
    """Runs planners and workers on Claude.

    Implements `engine.ports.AgentRunner`.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                import anthropic
            except ModuleNotFoundError as error:  # pragma: no cover - packaging guard
                raise RuntimeError(
                    "engine-adapter-anthropic requires the 'anthropic' package"
                ) from error
            client = anthropic.AsyncAnthropic()
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._max_iterations = max_iterations

    def start(self, spec: AgentSpec, invoke_tool: ToolInvoker) -> AnthropicAgentSession:
        return AnthropicAgentSession(
            self._client,
            spec,
            invoke_tool,
            model=spec.model or self._model,
            max_tokens=self._max_tokens,
            max_iterations=self._max_iterations,
        )


def build_agent_runner(**options: Any) -> AnthropicAgentRunner:
    """Plugin factory, registered under `engine.agent_runners` as 'anthropic'.

    Refuses rather than constructing a client that will fail on first use, so a
    caller walking a preference list falls through to the next backend cleanly.
    """
    if not credentials_available():
        raise RunnerUnavailable(
            "no Claude credentials; run `ant auth login` or set ANTHROPIC_API_KEY"
        )
    options.pop("scripts", None)  # not ours; other backends may take it
    return AnthropicAgentRunner(**options)


def format_tool_result(value: object) -> ToolResult:
    """Convenience for hosts returning structured data from a tool."""
    if isinstance(value, str):
        return ToolResult(value)
    return ToolResult(json.dumps(value, indent=2, sort_keys=True))


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "AnthropicAgentRunner",
    "AnthropicAgentSession",
    "build_agent_runner",
    "credentials_available",
    "format_tool_result",
]

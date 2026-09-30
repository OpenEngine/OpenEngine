"""ACP transport only; the QA workflow never branches on provider identity."""

import asyncio
import json
import os
from contextlib import aclosing
from pathlib import Path

from langgraph_acp import ACPEventType, ClaudeACPProvider, CodexACPProvider, StdioACPProvider

from open_verify.models import Decision


def provider_for(name: str, command: list[str] | None = None):
    if command:
        return StdioACPProvider(name=name, command=command)
    if name == "codex":
        return CodexACPProvider()
    if name == "claude":
        provider = ClaudeACPProvider()
        command = list(provider.command)
        if os.name == "nt" and command[0] == "npx":
            command[0] = "npx.cmd"
        return ClaudeACPProvider(command=command)
    raise ValueError("A custom agent requires --agent-command as a JSON argv array")


def parse_decision(message: str) -> Decision:
    text = message.strip()
    if text.startswith("```") and text.endswith("```") and "\n" in text:
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return Decision.model_validate_json(text)


class ACPDecisionAgent:
    def __init__(self, provider, workspace: Path, *, model=None, timeout=180):
        self.provider = provider
        self.workspace = workspace
        self.model = model
        self.timeout = timeout
        self.client = self.session = None

    async def decide(self, prompt: str) -> Decision:
        async with asyncio.timeout(self.timeout):
            if self.client is None:
                self.client = await self.provider.connect()
                self.session = await self.client.new_session(
                    cwd=self.workspace,
                    session_config={"model": self.model} if self.model else None,
                )
            for attempt in range(2):
                parts = []
                stop_reason = None
                native_tool_used = False
                async with aclosing(self.session.prompt(prompt)) as events:
                    async for event in events:
                        if event.type == ACPEventType.MESSAGE_DELTA:
                            content = event.data.get("content", {})
                            if content.get("type") == "text":
                                parts.append(content.get("text", ""))
                        elif event.type == ACPEventType.TOOL_STARTED:
                            # QA actions must go through host adapters so their observations
                            # are recorded and enforce the same limits for every provider.
                            if not native_tool_used:
                                await self.session.cancel()
                            native_tool_used = True
                        elif event.type == ACPEventType.ERROR:
                            raise RuntimeError(f"ACP agent error: {event.data}")
                        elif event.type == ACPEventType.PROMPT_COMPLETED:
                            stop_reason = event.data.get("stopReason")
                if native_tool_used:
                    if attempt:
                        raise RuntimeError(
                            "Agent repeatedly used a native tool instead of returning a QA action"
                        )
                    prompt = (
                        "Your previous turn was cancelled because it used a native tool. "
                        "Return only a JSON decision using the supplied host action schema. "
                        "Do not call native tools or assume the cancelled tool produced evidence.\n"
                        + prompt
                    )
                    continue
                if stop_reason != "end_turn":
                    raise RuntimeError(f"Agent did not finish its decision: {stop_reason}")
                try:
                    return parse_decision("".join(parts))
                except ValueError as exc:
                    if attempt:
                        raise ValueError(
                            f"Agent returned an invalid decision twice: {exc}"
                        ) from exc
                    prompt = (
                        "Your response was not a valid decision. Return only JSON matching this schema. "
                        "Do not call your own tools. Error: "
                        + str(exc)
                        + "\n"
                        + json.dumps(Decision.model_json_schema())
                    )
        raise RuntimeError("No decision returned")

    async def close(self):
        if self.client is not None:
            await self.client.close()

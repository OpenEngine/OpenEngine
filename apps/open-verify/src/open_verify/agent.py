"""ACP transport only; the QA workflow never branches on provider identity."""

import asyncio
import json
import os
import shutil
from contextlib import aclosing
from pathlib import Path

from langgraph_acp import ACPEventType, ClaudeACPProvider, CodexACPProvider, StdioACPProvider

from open_verify.contracts import Contract
from open_verify.models import Decision
from open_verify.visual import MAX_IMAGE_BYTES, VisualImage, VisualUnavailable

MAX_PROMPT_CHARS = 240_000
MAX_SESSION_CHARS = 600_000
RESPONSE_OPEN = "<open-verify-response>"
RESPONSE_CLOSE = "</open-verify-response>"


def provider_for(name: str, command: list[str] | None = None):
    if command:
        return StdioACPProvider(name=name, command=command)
    if name == "codex":
        # The adapter bundles a CLI that can lag behind the user's model config.
        # An explicit override wins; otherwise use the installed CLI when present.
        codex_path = os.environ.get("CODEX_PATH") or shutil.which("codex")
        return CodexACPProvider(env={"CODEX_PATH": codex_path} if codex_path else None)
    if name == "claude":
        provider = ClaudeACPProvider()
        command = list(provider.command)
        if os.name == "nt" and command[0] == "npx":
            command[0] = "npx.cmd"
        return ClaudeACPProvider(command=command)
    raise ValueError("A custom agent requires --agent-command as a JSON argv array")


def response_contract(prompt: str) -> str:
    """Add the portable structured-response contract to an ACP prompt."""
    return f"""{prompt}

Return the decision in exactly one response envelope. Its body must be one JSON
object matching the requested schema. Do not put commentary, Markdown, or
status text inside the envelope.

{RESPONSE_OPEN}
{{...}}
{RESPONSE_CLOSE}"""


def decision_payload(message: str) -> tuple[str, str | None]:
    """Extract a strict decision payload and any ACP transport notice.

    ACP streams ordinary agent-message text, including occasional provider
    notices. Delimiting the response prevents a notice from being parsed as a
    decision. Legacy bare JSON stays supported, but mixed prose and JSON is
    deliberately never guessed at.
    """
    text = message.strip()
    open_count = text.count(RESPONSE_OPEN)
    close_count = text.count(RESPONSE_CLOSE)
    if open_count or close_count:
        if open_count != 1 or close_count != 1:
            raise ValueError("response must contain exactly one decision envelope")
        start = text.index(RESPONSE_OPEN)
        end = text.index(RESPONSE_CLOSE)
        if end < start:
            raise ValueError("decision envelope closes before it opens")
        payload = text[start + len(RESPONSE_OPEN) : end].strip()
        notice = (text[:start] + text[end + len(RESPONSE_CLOSE) :]).strip()
        return payload, notice or None
    if text.startswith("```") and text.endswith("```") and "\n" in text:
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return text, None


def provider_failure(message: str) -> str | None:
    """Recognize an upstream error envelope streamed as ordinary ACP text.

    Some adapters emit a warning and a terminal JSON error instead of an ACP
    error event. Only a complete trailing error object is accepted here; this
    never extracts a decision from prose or treats application data as success.
    """
    if RESPONSE_OPEN in message or RESPONSE_CLOSE in message:
        return None
    lines = message.strip().splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.lstrip().startswith("{"):
            continue
        try:
            value = json.loads("".join(lines[index:]))
        except ValueError:
            continue
        if not isinstance(value, dict) or value.get("type") != "error":
            continue
        error = value.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            continue
        detail = error["message"][:2_000]
        if "model" in detail.lower() and "not supported" in detail.lower():
            detail += " Select a supported model with --model; omission inherits the provider's configured model."
        return detail
    return None


def parse_response(message: str, schema: type[Contract]) -> Contract:
    """Parse a closed typed response from the shared ACP decision protocol."""
    text, _ = decision_payload(message)
    return schema.model_validate_json(text)


def parse_decision(message: str) -> Decision:
    """Parse the planning protocol with the same closed-response handling as steps."""
    return parse_response(message, Decision)


class ACPDecisionAgent:
    def __init__(self, provider, workspace: Path, *, model=None, timeout=180):
        self.provider = provider
        self.workspace = workspace
        self.model = model
        self.timeout = timeout
        self.client = self.session = None
        self.session_chars = 0
        self.session_image_bytes = 0
        self.protocol_notices: list[str] = []

    async def reset_session(self):
        """Discard agent context without restarting the provider or host processes."""
        if self.session is not None:
            close = getattr(self.session, "close", None)
            if close is not None:
                await close()
        self.session = None
        self.session_chars = 0
        self.session_image_bytes = 0

    def record_protocol_notice(self, notice: str) -> None:
        """Keep bounded ACP diagnostics separate from the typed decision."""
        self.protocol_notices.append(notice[:2_000])
        del self.protocol_notices[:-20]

    async def decide(self, prompt: str) -> Decision:
        """Return a planning decision; step executors select their own response schema."""
        return await self.respond(prompt, Decision)

    async def respond(
        self,
        prompt: str,
        schema: type[Contract],
        *,
        on_call=lambda: None,
        image: VisualImage | None = None,
    ) -> Contract:
        """Account for every provider request, including bounded protocol-repair calls."""
        original_prompt = prompt
        prompt = response_contract(prompt)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError("QA context exceeds the bounded prompt budget")
        async with asyncio.timeout(self.timeout):
            if self.client is None:
                self.client = await self.provider.connect()
            if image is not None and not getattr(
                getattr(self.client, "capabilities", None), "prompt_image", False
            ):
                raise VisualUnavailable(
                    "VISUAL_INPUT_UNSUPPORTED",
                    "The configured ACP provider does not advertise image input",
                )
            if (
                self.session_chars + len(prompt) > MAX_SESSION_CHARS
                or self.session_image_bytes + (2 * len(image.data) if image else 0)
                > 2 * MAX_IMAGE_BYTES
            ):
                await self.reset_session()
            if self.session is None:
                self.session = await self.client.new_session(
                    cwd=self.workspace,
                    session_config={"model": self.model} if self.model else None,
                )
            for attempt in range(2):
                if len(prompt) > MAX_PROMPT_CHARS:
                    raise ValueError("QA recovery context exceeds the bounded prompt budget")
                self.session_chars += len(prompt)
                parts = []
                stop_reason = None
                native_tool_used = False
                on_call()
                payload = prompt
                if image is not None:
                    payload = [{"type": "text", "text": prompt}, image.content_block()]
                    self.session_image_bytes += len(image.data)
                async with aclosing(self.session.prompt(payload)) as events:
                    async for event in events:
                        if event.type == ACPEventType.MESSAGE_DELTA:
                            content = event.data.get("content", {})
                            if content.get("type") == "text":
                                parts.append(content.get("text", ""))
                                self.session_chars += len(content.get("text", ""))
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
                    await self.reset_session()
                    self.session = await self.client.new_session(
                        cwd=self.workspace,
                        session_config={"model": self.model} if self.model else None,
                    )
                    prompt = response_contract(
                        "Your previous turn was cancelled because it used a native tool. "
                        "Return a decision using the supplied host action schema. "
                        "Do not call native tools or assume the cancelled tool produced evidence.\n"
                        + "\nCurrent task:\n"
                        + original_prompt
                    )
                    continue
                if stop_reason != "end_turn":
                    raise RuntimeError(f"Agent did not finish its decision: {stop_reason}")
                response = "".join(parts)
                failure = provider_failure(response)
                if failure is not None:
                    raise RuntimeError(f"ACP provider failed: {failure}")
                try:
                    payload, notice = decision_payload(response)
                    if notice is not None:
                        self.record_protocol_notice(notice)
                    return schema.model_validate_json(payload)
                except ValueError as exc:
                    if attempt:
                        raise ValueError(
                            f"Agent returned an invalid decision twice: {exc}"
                        ) from exc
                    prompt = response_contract(
                        "Your response was not a valid decision. Return an enveloped JSON decision matching this schema. "
                        "Do not call your own tools. Error: "
                        + str(exc)[:2000]
                        + "\n"
                        + original_prompt
                    )
        raise RuntimeError("No decision returned")

    async def close(self):
        if self.client is not None:
            await self.client.close()

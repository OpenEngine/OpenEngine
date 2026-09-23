"""What Engine's settings mean to Claude's ACP adapter.

claude-agent-acp reads Claude Agent SDK options from `session/new` under
`_meta.claudeCode.options`, so tool lists, attribution and output style are
session settings here rather than command-line flags. The tool names and
capability grouping are Claude Code's own.
"""

from collections.abc import Iterable
from typing import Any

from engine.ports.agent_runner import ResponseStyle
from engine.ports.permissions import ApprovalCapability

#: Claude Code's own tools that only read. What an agent that never changes
#: anything is limited to, and what anyone may use unasked.
READ_ONLY_TOOLS = ("Read", "Glob", "Grep")

#: Claude's tools, by the Engine capability that covers them. Shell and MCP are
#: absent on purpose: see `allowed_tools_for`.
CAPABILITY_TOOLS: dict[ApprovalCapability, tuple[str, ...]] = {
    ApprovalCapability.READ: READ_ONLY_TOOLS,
    ApprovalCapability.EDIT: ("Edit", "Write", "NotebookEdit"),
    ApprovalCapability.WEB: ("WebFetch", "WebSearch"),
}

#: Claude Code's output-style names, by the Engine style that selects them. The
#: capitalization is part of the name: an unknown one silently keeps the default.
OUTPUT_STYLES: dict[ResponseStyle, str] = {
    ResponseStyle.CONCISE: "Concise",
    ResponseStyle.EXPLANATORY: "Explanatory",
    ResponseStyle.LEARNING: "Learning",
}


def allowed_tools_for(capabilities: Iterable[ApprovalCapability]) -> tuple[str, ...]:
    """The tools a capability set preapproves.

    Only capabilities whose whole answer is a tool name. A shell rule is written
    per command, so `Bash` always reaches the permission request, where
    `approvals.bash` is applied; an MCP grant names a server nobody has bound
    yet. Neither is refused -- both are decided one request at a time.
    """
    granted = set(capabilities)
    return tuple(
        tool
        for capability, tools in CAPABILITY_TOOLS.items()
        if capability in granted
        for tool in tools
    )


def _output_instructions(attribution: bool, output_style: ResponseStyle | None) -> str:
    instructions = []
    if not attribution:
        instructions.append(
            "Do not add AI attribution to commits, pull requests, or merge requests, "
            "including Co-authored-by trailers or Generated with Claude Code notices. "
            "This also applies to titles and descriptions sent through tools."
        )
    if output_style is ResponseStyle.CONCISE:
        instructions.append(
            "Keep responses and pull request or merge request descriptions concise. "
            "Lead with the result, include relevant validation, and omit unnecessary "
            "prefaces and repetition."
        )
    return "\n\n".join(instructions)


def claude_session_config(
    *,
    attribution: bool = True,
    output_style: ResponseStyle | None = None,
) -> dict[str, Any] | None:
    """ACP session metadata carrying Engine's attribution and style settings.

    `None` when both are at their defaults. A top-level `sessionConfig` would
    be silently ignored; the adapter reads `_meta.claudeCode.options` on
    `session/new` and `session/load`.
    """
    settings: dict[str, Any] = {}
    if not attribution:
        settings["attribution"] = {"commit": "", "pr": "", "sessionUrl": False}
    if output_style is not None:
        settings["outputStyle"] = OUTPUT_STYLES[output_style]
    if not settings:
        return None
    options: dict[str, Any] = {"settings": settings}
    instructions = _output_instructions(attribution, output_style)
    if instructions:
        options["systemPrompt"] = {
            "type": "preset",
            "preset": "claude_code",
            "append": instructions,
        }
    return {"claudeCode": {"options": options}}


__all__ = [
    "CAPABILITY_TOOLS",
    "OUTPUT_STYLES",
    "READ_ONLY_TOOLS",
    "allowed_tools_for",
    "claude_session_config",
]

"""Outbound feedback tool: a validated host callback behind a stdio MCP server.

Deliberately independent of the Slack broker rather than a mode of it. The two
grant different authority -- Slack's tool starts work, this one may only steer
work that already exists -- and a shared implementation would mean every change
to one conversation's authority is a change to the other's. What they do share,
and take from `engine.single_tool_mcp`, is the transport underneath: carrying a
call to the host decides nothing about who may make it.

The host binds the pull request the feedback belongs to. The agent supplies
only the feedback text: it cannot choose which work order hears it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from engine.single_tool_mcp import SingleToolBroker, serve_from_command_line
from langgraph_acp.permissions import ACPPermissionOutcome, ACPPermissionRequest

#: Given a prompt, forward it to this pull request's work order and return
#: (url, run_id).
SteerWorkorder = Callable[[str], Awaitable[tuple[str, str]]]

_SERVER_NAME = "concierge"
_SERVER_INFO_NAME = "engine-concierge"

FEEDBACK_TOOL_NAME = "continue_workorder"

_TOOL_SPEC: dict[str, object] = {
    "name": FEEDBACK_TOOL_NAME,
    "description": (
        "Send feedback to the work order that opened this pull request, so the "
        "agent behind it can act on the request. Never starts a new work order. "
        "Calling this is the only way to affect the pull request: the reply "
        "posted there is fixed text chosen by whether this succeeded."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "The feedback the work order should act on.",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}


class FeedbackBroker(SingleToolBroker):
    """Expose ``continue_workorder`` to a provider CLI over a local MCP server.

    The factory that creates it binds the callback that reaches the work order,
    so the broker itself has no opinion about runtimes or runs -- it validates
    the call, forwards it, and returns the answer.
    """

    entry_point = "engine.github_concierge.github_egress"
    server_name = _SERVER_NAME

    def __init__(self, *, steer_workorder: SteerWorkorder) -> None:
        super().__init__()
        self._steer_workorder = steer_workorder

    async def _submit(self, request: object) -> dict[str, object]:
        refusal = self._credentialled(request, FEEDBACK_TOOL_NAME)
        if refusal is not None:
            return refusal
        arguments = request["arguments"]  # type: ignore[index]
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "prompt must be a non-empty string"}
        if set(arguments) - {"prompt"}:
            return {"ok": False, "error": "unknown feedback arguments"}
        try:
            url, run_id = await self._steer_workorder(prompt.strip())
        except Exception as error:
            return {"ok": False, "error": f"could not deliver the feedback: {error}"}
        return {
            "ok": True,
            "text": f"Feedback delivered to work order `{run_id}`.",
            "data": {"run_id": run_id, "url": url},
        }


def main() -> None:
    serve_from_command_line(
        tool_spec=_TOOL_SPEC, server_info_name=_SERVER_INFO_NAME
    )


async def tool_permission(request: ACPPermissionRequest) -> ACPPermissionOutcome:
    """Approve only the one named MCP grant; decline all other operations."""

    names = {f"mcp__concierge__{FEEDBACK_TOOL_NAME}", f"concierge/{FEEDBACK_TOOL_NAME}"}
    if any(isinstance(value, str) and value in names
           for value in (request.tool_call.get(field) for field in ("name", "toolName", "title"))):
        for option in request.options:
            if option.kind == "allow_once":
                return ACPPermissionOutcome.selected(option.option_id)
    return ACPPermissionOutcome.cancelled()


__all__ = [
    "FEEDBACK_TOOL_NAME",
    "FeedbackBroker",
    "tool_permission",
]


if __name__ == "__main__":
    main()

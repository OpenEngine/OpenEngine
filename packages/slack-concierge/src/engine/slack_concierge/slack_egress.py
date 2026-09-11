"""Outbound work-order tool: validated host callback exposed through stdio MCP.

The host binds the Slack origin and posts status and links through its notifier.
The agent supplies only a repository and task; it cannot redirect those replies.

What an agent may ask for here, and what it gets back, is this surface's own --
the pull-request concierge grants different authority through a broker of its
own. Only the transport beneath the two is shared, from
`engine.single_tool_mcp`, because carrying a call decides nothing about who may
make it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from engine.single_tool_mcp import SingleToolBroker, serve_from_command_line
from langgraph_acp.permissions import ACPPermissionOutcome, ACPPermissionRequest

#: Given (repository, prompt) create a work order and return (url, run_id).
CreateWorkorder = Callable[[str, str], Awaitable[tuple[str, str]]]

_SERVER_NAME = "concierge"
_SERVER_INFO_NAME = "engine-concierge"

CONCIERGE_TOOL_NAME = "create_workorder"

_TOOL_SPEC: dict[str, object] = {
    "name": CONCIERGE_TOOL_NAME,
    "description": (
        "Start a new work order. The work order runs a workflow that "
        "implements the requested task, and status updates will be posted "
        "in this conversation. Returns the work order URL so you can share "
        "it with the user."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "What the work order should accomplish.",
            },
        },
        "required": ["prompt"],
        "additionalProperties": False,
    },
}


class ConciergeBroker(SingleToolBroker):
    """Expose ``create_workorder`` to a provider CLI over a local MCP server.

    The factory that creates it binds the callback that actually starts the
    work order, so the broker itself has no opinion about workflows, runners,
    or repositories -- it validates the call, forwards it, and returns the
    answer.
    """

    entry_point = "engine.slack_concierge.slack_egress"
    server_name = _SERVER_NAME

    def __init__(
        self,
        *,
        create_workorder: CreateWorkorder,
        default_repository: str = "",
    ) -> None:
        super().__init__()
        self._create_workorder = create_workorder
        self._default_repository = default_repository

    async def _submit(self, request: object) -> dict[str, object]:
        refusal = self._credentialled(request, CONCIERGE_TOOL_NAME)
        if refusal is not None:
            return refusal
        arguments = request["arguments"]  # type: ignore[index]
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "prompt must be a non-empty string"}
        if set(arguments) - {"prompt"}:
            return {"ok": False, "error": "unknown work-order arguments"}
        repository = self._default_repository or "."
        try:
            url, run_id = await self._create_workorder(repository, prompt.strip())
        except Exception as error:
            return {"ok": False, "error": f"could not start the work order: {error}"}
        return {
            "ok": True,
            "text": (
                f"Work order `{run_id}` started on `{repository}`. "
                "Status updates will appear in this thread."
            ),
            "data": {"run_id": run_id, "url": url, "repository": repository},
        }


def main() -> None:
    serve_from_command_line(
        tool_spec=_TOOL_SPEC, server_info_name=_SERVER_INFO_NAME
    )


async def tool_permission(request: ACPPermissionRequest) -> ACPPermissionOutcome:
    """Approve only the one named MCP grant; decline all other operations."""

    names = {"mcp__concierge__create_workorder", "concierge/create_workorder"}
    if any(isinstance(value, str) and value in names
           for value in (request.tool_call.get(field) for field in ("name", "toolName", "title"))):
        for option in request.options:
            if option.kind == "allow_once":
                return ACPPermissionOutcome.selected(option.option_id)
    return ACPPermissionOutcome.cancelled()


__all__ = [
    "CONCIERGE_TOOL_NAME",
    "ConciergeBroker",
    "tool_permission",
]


if __name__ == "__main__":
    main()

"""Outbound steering tool: a validated host callback behind a stdio MCP server.

A second server beside the work-order one rather than a second tool on it.
`create_workorder` starts work; this one redirects work that is already
running, and the transport underneath both is written for exactly one tool
because a broker serving several would have to say which of its grants a call
is reaching for. Two brokers answer that by construction: one grant each, one
credential each, and one decision each about who may use it.

What the host binds here is the conversation, not the run. The agent names a
run id, because a thread can have started more than one work order and only the
conversation knows which one a follow-up is about -- so the name comes from an
untrusted Slack message and is a request rather than a permission. The callback
the host binds is where a named run is checked against the thread that is
asking; nothing in this module knows what makes a run reachable.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from engine.single_tool_mcp import SingleToolBroker, serve_from_command_line

#: Given (run_id, prompt) steer that work order and return (url, run_id).
SteerWorkorder = Callable[[str, str], Awaitable[tuple[str, str]]]

STEER_SERVER_NAME = "steering"
_SERVER_INFO_NAME = "engine-steering"

STEER_TOOL_NAME = "steer_workorder"

_TOOL_SPEC: dict[str, object] = {
    "name": STEER_TOOL_NAME,
    "description": (
        "Send a follow-up instruction to a work order that is already running, "
        "so it acts on the new request instead of starting the task again. Use "
        "this when the user refines or corrects a work order this conversation "
        "started; use create_workorder for anything else. Only work orders "
        "started in this conversation can be steered."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "The work order to steer, as returned by create_workorder."
                ),
            },
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "The follow-up the work order should act on.",
            },
        },
        "required": ["run_id", "prompt"],
        "additionalProperties": False,
    },
}


class SteeringBroker(SingleToolBroker):
    """Expose ``steer_workorder`` to a provider CLI over a local MCP server.

    The factory that creates it binds the callback that reaches the runtime, so
    the broker itself has no opinion about runs or which of them this
    conversation may touch -- it validates the shape of the call, forwards it,
    and returns the answer.
    """

    entry_point = "engine.slack_concierge.slack_steering"
    server_name = STEER_SERVER_NAME

    def __init__(self, *, steer_workorder: SteerWorkorder) -> None:
        super().__init__()
        self._steer_workorder = steer_workorder

    async def _submit(self, request: object) -> dict[str, object]:
        refusal = self._credentialled(request, STEER_TOOL_NAME)
        if refusal is not None:
            return refusal
        arguments = request["arguments"]  # type: ignore[index]
        run_id = arguments.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            return {"ok": False, "error": "run_id must be a non-empty string"}
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "prompt must be a non-empty string"}
        if set(arguments) - {"run_id", "prompt"}:
            return {"ok": False, "error": "unknown steering arguments"}
        try:
            url, steered = await self._steer_workorder(run_id.strip(), prompt.strip())
        except Exception as error:
            # The message is the host's refusal or failure, which is the part
            # worth telling the agent: it says whether asking again, or asking
            # for something else, is worth trying.
            return {"ok": False, "error": f"could not steer the work order: {error}"}
        return {
            "ok": True,
            "text": (
                f"Sent to work order `{steered}`, which is already running. "
                "Status updates will appear in this thread."
            ),
            "data": {"run_id": steered, "url": url},
        }


def main() -> None:
    serve_from_command_line(
        tool_spec=_TOOL_SPEC, server_info_name=_SERVER_INFO_NAME
    )


__all__ = [
    "STEER_SERVER_NAME",
    "STEER_TOOL_NAME",
    "SteerWorkorder",
    "SteeringBroker",
]


if __name__ == "__main__":
    main()

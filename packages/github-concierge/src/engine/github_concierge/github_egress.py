"""Outbound feedback tool: a validated host callback behind a stdio MCP server.

Deliberately independent of the Slack broker rather than a mode of it. The two
grant different authority -- Slack's tool is told which repository to work in,
this one may only reach the pull request the comment arrived on -- and a shared
implementation would mean every change to one conversation's authority is a
change to the other's. What they do share, and take from
`engine.single_tool_mcp`, is the transport underneath: carrying a call to the
host decides nothing about who may make it.

The host binds the pull request the feedback belongs to, and chooses what
reaching it means: steering the work order already in flight for that pull
request, or starting one when none is. The agent supplies the feedback text and
the acknowledgement posted once it lands: it cannot choose which work order
hears it, nor whether one is started.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from engine.single_tool_mcp import SingleToolBroker, serve_from_command_line
from langgraph_acp.permissions import allow_mcp_tools


@dataclass(frozen=True, slots=True)
class Continuation:
    """The work order a comment reached, and whether it was started for it.

    ``started`` is the host's answer rather than the agent's: which of the two
    happened is decided by what this process already knew about the pull
    request, and it is what the public reply is chosen by.
    """

    url: str
    run_id: str
    started: bool = False


#: Given a prompt and the acknowledgement to post once it lands, forward the
#: prompt to this pull request's work order -- the one in flight, or one
#: started for it -- and say which was reached.
ContinueWorkorder = Callable[[str, str], Awaitable[Continuation]]

#: Long enough to restate a request, short enough to stay an acknowledgement.
ACKNOWLEDGEMENT_LIMIT = 500

_SERVER_NAME = "concierge"
_SERVER_INFO_NAME = "engine-concierge"

FEEDBACK_TOOL_NAME = "continue_workorder"

_TOOL_SPEC: dict[str, object] = {
    "name": FEEDBACK_TOOL_NAME,
    "description": (
        "Send feedback to the work order for this pull request, so the agent "
        "behind it can act on the request. The host decides where that lands: "
        "the work order already in flight for this pull request, or a new one "
        "started for it when none is. Calling this is the only way to affect "
        "the pull request: when it succeeds, the acknowledgement is posted "
        "there as the reply."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "minLength": 1,
                "description": "The feedback the work order should act on.",
            },
            "acknowledgement": {
                "type": "string",
                "maxLength": ACKNOWLEDGEMENT_LIMIT,
                "description": (
                    "One or two sentences posted publicly on the pull request "
                    "once the feedback lands: acknowledge the request and "
                    "restate what will be done, e.g. 'On it: renaming the "
                    "`retry` flag to `max_retries` and updating its callers.' "
                    "Restate only the request. Any identifier, path, number, "
                    "or other literal must be quoted from the comment, or "
                    "fixed text is posted instead."
                ),
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

    def __init__(self, *, continue_workorder: ContinueWorkorder) -> None:
        super().__init__()
        self._continue_workorder = continue_workorder

    async def _submit(self, request: object) -> dict[str, object]:
        refusal = self._credentialled(request, FEEDBACK_TOOL_NAME)
        if refusal is not None:
            return refusal
        arguments = request["arguments"]  # type: ignore[index]
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "prompt must be a non-empty string"}
        acknowledgement = arguments.get("acknowledgement", "")
        if not isinstance(acknowledgement, str):
            return {"ok": False, "error": "acknowledgement must be a string"}
        if len(acknowledgement.strip()) > ACKNOWLEDGEMENT_LIMIT:
            return {
                "ok": False,
                "error": f"acknowledgement must be at most {ACKNOWLEDGEMENT_LIMIT} characters",
            }
        if set(arguments) - {"prompt", "acknowledgement"}:
            return {"ok": False, "error": "unknown feedback arguments"}
        try:
            reached = await self._continue_workorder(prompt.strip(), acknowledgement.strip())
        except Exception as error:
            return {"ok": False, "error": f"could not deliver the feedback: {error}"}
        delivered = (
            f"Started work order `{reached.run_id}` for this pull request."
            if reached.started
            else f"Feedback delivered to work order `{reached.run_id}`."
        )
        return {
            "ok": True,
            "text": delivered,
            "data": {
                "run_id": reached.run_id,
                "url": reached.url,
                "started": reached.started,
            },
        }


def main() -> None:
    serve_from_command_line(
        tool_spec=_TOOL_SPEC, server_info_name=_SERVER_INFO_NAME
    )


#: Approve only the one named MCP grant; decline all other operations.
tool_permission = allow_mcp_tools("concierge", (FEEDBACK_TOOL_NAME,))


__all__ = [
    "FEEDBACK_TOOL_NAME",
    "Continuation",
    "FeedbackBroker",
    "tool_permission",
]


if __name__ == "__main__":
    main()

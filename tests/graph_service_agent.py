"""An ACP agent for the graph service tests, driven by what it is asked.

A real child process, like the other stub agents, so that a registered graph's
nodes are driven over ACP exactly as the daemon drives them. Behaviour is
chosen by the prompt rather than by flags, because one registry serves every
node of a graph and the nodes are told different things:

    ...           -> answer "echo: <prompt>", reporting $STUB_COST dollars
    WAIT          -> work until cancelled, the way a long turn is steered
    AUTH          -> refuse the prompt the way an agent with no login does
"""

import json
import os
import sys
import uuid
from typing import Any


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def receive() -> dict[str, Any] | None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        if line.strip():
            return json.loads(line)


def update(session_id: str, payload: dict[str, Any]) -> None:
    send({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": payload},
    })


def text_of(prompt: list[dict[str, Any]]) -> str:
    return "".join(block.get("text", "") for block in prompt if block.get("type") == "text")


def answer(message_id: Any, session_id: str, text: str) -> None:
    update(session_id, {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": f"echo: {text}"},
    })
    COST[session_id] = COST.get(session_id, 0.0) + float(os.environ.get("STUB_COST", "0.5"))
    update(session_id, {
        "sessionUpdate": "usage_update",
        "used": 0,
        "size": 1,
        "cost": {"amount": COST[session_id], "currency": "USD"},
    })
    send({"jsonrpc": "2.0", "id": message_id, "result": {"stopReason": "end_turn"}})


COST: dict[str, float] = {}


def main() -> None:
    while True:
        message = receive()
        if message is None:
            return
        method, message_id = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": message_id, "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"loadSession": True},
                "authMethods": [],
            }})
        elif method in ("session/new", "session/load"):
            session_id = params.get("sessionId") or f"sess-{uuid.uuid4().hex[:8]}"
            send({"jsonrpc": "2.0", "id": message_id, "result": {"sessionId": session_id}})
        elif method == "session/prompt":
            session_id = params["sessionId"]
            text = text_of(params.get("prompt") or [])
            if "AUTH" in text:
                send({"jsonrpc": "2.0", "id": message_id, "error": {
                    "code": -32000, "message": "Authentication required",
                }})
            elif "WAIT" in text:
                update(session_id, {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "working..."},
                })
                while True:
                    other = receive()
                    if other is None:
                        return
                    if other.get("method") == "session/cancel":
                        send({"jsonrpc": "2.0", "id": message_id, "result": {"stopReason": "cancelled"}})
                        break
            else:
                answer(message_id, session_id, text)
        elif message_id is not None:
            send({"jsonrpc": "2.0", "id": message_id, "result": {}})


if __name__ == "__main__":
    main()

"""An ACP agent that does nothing, correctly.

Launched as a real child process by the client tests, because the thing under
test is a process boundary: pipes, framing, a handshake, and a shutdown. A mock
client object would exercise none of it and would agree with whatever the
implementation happened to do.

Behaviour is chosen by command-line flags so one script can play every agent a
test needs -- one that cannot resume, one that asks permission, one that dies
mid-request. Every message it receives is appended to `$FAKE_AGENT_LOG`, which
is how a test asserts on what was *sent* rather than only on what came back.
"""

import json
import os
import sys
from typing import Any

#: The ids the agent uses for the requests it makes of the client.
PERMISSION_REQUEST_ID = 9001
READ_FILE_REQUEST_ID = 9002

SESSION_ID = "sess_fake_1"


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def respond(message_id: Any, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": message_id, "result": result})


def fail(message_id: Any, code: int, text: str) -> None:
    send({"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": text}})


def update(payload: dict[str, Any]) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SESSION_ID, "update": payload},
        }
    )


def receive() -> dict[str, Any] | None:
    """The next message, recorded on the way past. `None` at end of input."""
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        message: dict[str, Any] = json.loads(stripped)
        path = os.environ.get("FAKE_AGENT_LOG")
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(message) + "\n")
        return message


def ask(request_id: int, method: str, params: dict[str, Any]) -> None:
    """Call the client, wait for its answer, and report it as a tool update.

    Reporting the answer back through the session stream is what lets a test
    see what the client replied without reaching inside it.
    """
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    while True:
        reply = receive()
        if reply is None:
            return
        if reply.get("id") == request_id:
            update(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call_1",
                    "answer": reply.get("result"),
                    "refusal": reply.get("error"),
                }
            )
            return


def capabilities(options: set[str]) -> dict[str, Any]:
    return {
        "protocolVersion": 1,
        "agentCapabilities": {
            "loadSession": "--no-resume" not in options,
            "promptCapabilities": {"image": True, "embeddedContext": True},
            "mcpCapabilities": {"http": True},
        },
        "authMethods": [{"id": "oauth", "name": "Log in"}],
        "_futureCapability": "kept in raw",
    }


def run_turn(message_id: Any, options: set[str]) -> None:
    """One prompt turn: some updates, maybe a question, then a stop reason."""
    response = os.environ.get("FAKE_AGENT_RESPONSE", "Looking.")
    message_chunks = (response[:4], response[4:]) if "--split-message" in options else (response,)
    for text in message_chunks:
        update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            }
        )
    update({"sessionUpdate": "a_kind_invented_after_this_release", "detail": 1})
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SESSION_ID, "update": "not an object at all"},
        }
    )

    if "--permission" in options:
        ask(
            PERMISSION_REQUEST_ID,
            "session/request_permission",
            {
                "sessionId": SESSION_ID,
                "toolCall": {"toolCallId": "call_1"},
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}
                ],
            },
        )

    if "--read-file" in options:
        ask(
            READ_FILE_REQUEST_ID,
            "fs/read_text_file",
            {"sessionId": SESSION_ID, "path": "/etc/hosts"},
        )

    if "--slow" in options:
        # The turn ends only when the client says to stop, which is how a test
        # observes cancellation rather than racing it.
        while True:
            message = receive()
            if message is None:
                return
            if message.get("method") == "session/cancel":
                respond(message_id, {"stopReason": "cancelled"})
                return

    respond(message_id, {"stopReason": "end_turn"})


def main() -> int:
    options = set(sys.argv[1:])
    while True:
        message = receive()
        if message is None:
            return 0
        method = message.get("method")
        if method is None:
            continue  # An answer to something this agent asked.
        message_id = message.get("id")

        if method == "initialize":
            respond(message_id, capabilities(options))
        elif method == "session/new":
            if "--die-on-new-session" in options:
                print("codex-acp: everything is on fire", file=sys.stderr, flush=True)
                return 3
            if "--nameless-session" in options:
                respond(message_id, {})
            else:
                respond(message_id, {"sessionId": SESSION_ID})
        elif method == "session/load":
            update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "replayed history"},
                }
            )
            respond(message_id, None)
        elif method == "session/prompt":
            if "--refuse-prompt" in options:
                fail(message_id, -32000, "this session is over quota")
            else:
                run_turn(message_id, options)
        elif method == "session/cancel":
            continue
        elif message_id is not None:
            fail(message_id, -32601, f"no such method: {method}")


if __name__ == "__main__":
    sys.exit(main())

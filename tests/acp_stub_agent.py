"""An ACP agent that keeps its own conversation, on disk, across restarts.

Launched as a real child process, because the thing being tested is a process
boundary that survives being crossed twice: the agent asks for permission, the
worker driving it dies, and a different worker picks the conversation back up.
A stub object inside the test would agree with whatever the implementation did
and would prove nothing about either crossing.

The one behaviour worth spelling out is what makes the handoff testable:

    session/new      -> a session id, and a file to keep its history in
    session/prompt   -> ask permission, and *remember that it is unanswered*
    <the client dies without answering>
    session/load     -> the same id, the same history, still unanswered
    session/prompt   -> ask the same question again

An agent that forgot the outstanding request on reload would let a broken
handoff pass: the continuation prompt would look like a fresh turn and nobody
would notice the first one had been dropped. Asking again is also what a real
agent does -- it is mid-tool-call, and loading a session restores that.

Everything received is appended to `$STUB_ACP_LOG`, which is how a test asserts
on what was *sent*: that `session/new` happened once, that the original prompt
was delivered once, and that the continuation prompt is not it.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

#: The id the agent uses for the permission request it makes of the client.
PERMISSION_REQUEST_ID = 7001

#: What the agent says once it has been allowed to proceed.
DONE = "Ran the command."

#: And once it has been refused.
REFUSED = "Stopped, as asked."

#: What the agent says *before* it calls a tool, under `STUB_ACP_NARRATE`.
NARRATION = "I'll start by reading the tests."

#: And what it says before asking permission, under the same switch. Separate
#: from `NARRATION` so a test can tell which of the two it is looking at.
ASK_NARRATION = "I'll run the tests now."

#: The call it narrates, and what it is called.
NARRATED_CALL = "call_read"
NARRATED_TOOL = "Read tests"


def state_dir() -> Path:
    return Path(os.environ["STUB_ACP_STATE"])


def session_file(session_id: str) -> Path:
    return state_dir() / f"{session_id}.json"


def load(session_id: str) -> dict[str, Any]:
    path = session_file(session_id)
    if not path.exists():
        return {"turns": [], "awaiting_permission": False}
    return json.loads(path.read_text())


def save(session_id: str, session: dict[str, Any]) -> None:
    session_file(session_id).write_text(json.dumps(session))


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def respond(message_id: Any, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": message_id, "result": result})


def update(session_id: str, payload: dict[str, Any]) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": payload},
        }
    )


def say(session_id: str, text: str) -> None:
    update(
        session_id,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
    )


def narrate_a_tool_call(session_id: str) -> None:
    """Say what it is about to do, do it, and report that it finished.

    The shape of a real turn: an agent writes a line, calls a tool, and only
    then writes the next line. A stub that only ever speaks at the end cannot
    tell an ordering bug from a correct implementation.
    """
    say(session_id, NARRATION)
    update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": NARRATED_CALL,
            "title": NARRATED_TOOL,
            "kind": "read",
            "status": "pending",
        },
    )
    update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": NARRATED_CALL,
            "title": NARRATED_TOOL,
            "status": "completed",
        },
    )


def exercise_mcp(session_id: str) -> None:
    """Launch the supplied stdio server and cross its broker boundary."""
    session = load(session_id)
    servers = session.get("mcp_servers") or []
    if not servers:
        raise RuntimeError("the ACP session received no MCP server")
    server = servers[0]
    process = subprocess.Popen(
        [server["command"], *server.get("args", [])],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    def call(message: dict[str, Any]) -> dict[str, Any]:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            assert process.stderr is not None
            raise RuntimeError(process.stderr.read() or "MCP server exited")
        return json.loads(line)

    listed = call({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
    called = call(
        {
            "jsonrpc": "2.0",
            "id": "clarify",
            "method": "tools/call",
            "params": {"name": "clarify", "arguments": {}},
        }
    )
    if not session.get("mcp_tools"):
        review = call(
            {
                "jsonrpc": "2.0",
                "id": "review",
                "method": "tools/call",
                "params": {
                    "name": "open_pull_request",
                    "arguments": {
                        "branch": "agent/graph-tools",
                        "title": "feat: add graph tools",
                        "body": "Test body.",
                    },
                },
            }
        )
        session["mcp_review"] = review
    session.setdefault("mcp_tools", []).append(
        [tool["name"] for tool in listed["result"]["tools"]]
    )
    args = server.get("args", [])
    session.setdefault("mcp_ports", []).append(args[args.index("--port") + 1])
    session.setdefault("mcp_clarified", []).append(called)
    save(session_id, session)
    if os.environ.get("STUB_ACP_MCP_GIT"):
        session["mcp_git"] = call(
            {
                "jsonrpc": "2.0",
                "id": "git",
                "method": "tools/call",
                "params": {
                    "name": "git_subcommand",
                    "arguments": {"arguments": ["status", "--short"]},
                },
            }
        )
        save(session_id, session)
    process.stdin.close()
    return_code = process.wait(timeout=5)
    if return_code:
        assert process.stderr is not None
        raise RuntimeError(process.stderr.read() or f"MCP exited {return_code}")


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
        log = os.environ.get("STUB_ACP_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(message) + "\n")
        return message


def capabilities() -> dict[str, Any]:
    return {
        "protocolVersion": 1,
        "agentCapabilities": {
            "loadSession": True,
            "promptCapabilities": {"image": False, "embeddedContext": False},
        },
    }


def ask_permission(session_id: str) -> dict[str, Any] | None:
    """Ask, and block until the client answers or the pipe closes."""
    send(
        {
            "jsonrpc": "2.0",
            "id": PERMISSION_REQUEST_ID,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": "call_1",
                    "title": "run the tests",
                    "kind": "execute",
                    "rawInput": {"command": "pytest"},
                },
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    while True:
        reply = receive()
        if reply is None:
            return None
        if reply.get("id") == PERMISSION_REQUEST_ID:
            outcome = (reply.get("result") or {}).get("outcome") or {}
            return outcome if isinstance(outcome, dict) else {}


def run_turn(message_id: Any, session_id: str, prompt_text: str) -> None:
    session = load(session_id)
    session["turns"].append(prompt_text)
    save(session_id, session)
    if os.environ.get("STUB_ACP_USE_MCP"):
        exercise_mcp(session_id)
        session = load(session_id)
    if os.environ.get("STUB_ACP_WAIT_FOR_CANCEL") and not session.get("cancelled"):
        save(session_id, session)
        while message := receive():
            params = message.get("params") or {}
            if (
                message.get("method") == "session/cancel"
                and params.get("sessionId") == session_id
            ):
                session["cancelled"] = True
                save(session_id, session)
                respond(message_id, {"stopReason": "cancelled"})
                return
    # `STUB_ACP_ASK_EVERY` is what a real agent doing real work looks like: each
    # turn wants to run something of its own, so each turn asks again. It is the
    # only way to hold a *steered* turn open long enough for a test to say
    # something into it.
    asks = os.environ.get("STUB_ACP_ASK") and (
        os.environ.get("STUB_ACP_ASK_EVERY") or not session.get("granted")
    )
    if asks:
        # Written down *before* asking, so a client that dies without answering
        # leaves an agent that still knows it was interrupted mid-tool-call.
        session["awaiting_permission"] = True
        save(session_id, session)
        # Said before the question, the way an agent explains what it is about
        # to ask for. Nothing else is sent until the client answers.
        if os.environ.get("STUB_ACP_NARRATE"):
            say(session_id, ASK_NARRATION)
        outcome = ask_permission(session_id)
        if outcome is None:
            return  # The client went away. The file remembers where we were.
        session = load(session_id)
        session["awaiting_permission"] = False
        if outcome.get("outcome") != "selected" or outcome.get("optionId") != "allow":
            session["refused"] = True
            save(session_id, session)
            say(session_id, REFUSED)
            respond(message_id, {"stopReason": "refusal"})
            return
        session["granted"] = True
        save(session_id, session)
        say(session_id, DONE)
        respond(message_id, {"stopReason": "end_turn"})
        return
    save(session_id, session)
    if os.environ.get("STUB_ACP_NARRATE"):
        narrate_a_tool_call(session_id)
    say(session_id, os.environ.get("STUB_ACP_RESPONSE", DONE))
    respond(message_id, {"stopReason": "end_turn"})


def prompt_text(params: dict[str, Any]) -> str:
    return "".join(
        str(block.get("text", ""))
        for block in params.get("prompt", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def main() -> int:
    while True:
        message = receive()
        if message is None:
            return 0
        method = message.get("method")
        if method is None:
            continue  # An answer to something this agent asked.
        message_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            respond(message_id, capabilities())
        elif method == "session/new":
            session_id = f"sess_{uuid.uuid4().hex[:8]}"
            save(
                session_id,
                {
                    "turns": [],
                    "awaiting_permission": False,
                    "mcp_servers": params.get("mcpServers", []),
                },
            )
            respond(message_id, {"sessionId": session_id})
        elif method == "session/load":
            session_id = str(params.get("sessionId"))
            session = load(session_id)
            session["loads"] = session.get("loads", 0) + 1
            session["mcp_servers"] = params.get("mcpServers", [])
            save(session_id, session)
            say(session_id, "Restored the conversation.")
            respond(message_id, None)
        elif method == "session/prompt":
            run_turn(message_id, str(params.get("sessionId")), prompt_text(params))
        elif method == "session/cancel":
            continue
        elif message_id is not None:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {"code": -32601, "message": f"no such method: {method}"},
                }
            )


if __name__ == "__main__":
    sys.exit(main())

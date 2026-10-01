"""An ACP agent that does nothing, correctly.

Launched as a real child process by the client tests, because the thing under
test is a process boundary: pipes, framing, a handshake, and a shutdown. A mock
client object would exercise none of it and would agree with whatever the
implementation happened to do.

Behaviour is chosen by command-line flags so one script can play every agent a
test needs -- one that cannot resume, one that asks permission, one that dies
mid-request. Every message it receives is appended to `$FAKE_AGENT_LOG`, which
is how a test asserts on what was *sent* rather than only on what came back.

`--run-directive` is the one that does something: it runs the shell command
named after the last `run:` in the prompt, in the session's working directory,
once the client has allowed it. That is what lets an approval be asserted on
the file it did or did not create, rather than on the answer that was sent.

`--ask` puts a question to the client the way claude-agent-acp does, as a form
`elicitation/create` -- and, like it, only to a client that advertised forms --
then writes whatever came back to `answer.json` in the session's directory.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

#: The ids the agent uses for the requests it makes of the client.
PERMISSION_REQUEST_ID = 9001
READ_FILE_REQUEST_ID = 9002
ELICITATION_REQUEST_ID = 9003

SESSION_ID = "sess_fake_1"

#: What `--run-directive` reads the command from: the text after the last one.
DIRECTIVE = "run:"

#: The working directory each session was opened in, for `--run-directive`.
WORKING_DIRECTORIES: dict[str, str] = {}

#: Commands allowed with `allow_always`, which this process does not ask again.
ALWAYS_ALLOWED: set[str] = set()

#: What the client said it could do in `initialize`.
CLIENT_CAPABILITIES: dict[str, Any] = {}

#: The question `--ask` puts, in claude-agent-acp's form: an option list, and a
#: free-text field beside it for an answer that is none of them.
QUESTION_FORM = {
    "type": "object",
    "properties": {
        "question_0": {
            "type": "string",
            "title": "Colour",
            "oneOf": [
                {"const": "Red", "title": "Red", "description": "Warm"},
                {"const": "Blue", "title": "Blue"},
            ],
        },
        "question_0_custom": {
            "type": "string",
            "title": "Other",
            "_meta": {
                "_askUserQuestionCustomAnswer": {
                    "questionId": "question_0",
                    "isCustomAnswer": True,
                }
            },
        },
    },
}


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def respond(message_id: Any, result: Any) -> None:
    send({"jsonrpc": "2.0", "id": message_id, "result": result})


def fail(message_id: Any, code: int, text: str, data: Any = None) -> None:
    error: dict[str, Any] = {"code": code, "message": text}
    if data is not None:
        error["data"] = data
    send({"jsonrpc": "2.0", "id": message_id, "error": error})


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


def run_directive(message_id: Any, params: dict[str, Any]) -> None:
    """Ask to run the prompt's `run:` command, and run it only if allowed.

    Refused, the turn ends without running anything; allowed for good, the same
    command is not asked about again by this process -- which is all an agent's
    own "always" can remember, and why a grant has to outlive the process
    somewhere else.
    """
    prompt = "".join(
        str(block.get("text", ""))
        for block in params.get("prompt") or ()
        if isinstance(block, dict) and block.get("type") == "text"
    )
    lines = [line for line in prompt.splitlines() if DIRECTIVE in line]
    if not lines:
        fail(message_id, -32602, f"no {DIRECTIVE!r} directive in the prompt")
        return
    command = lines[-1].split(DIRECTIVE, 1)[1].strip()
    call = {
        "toolCallId": "call_run",
        "title": command,
        "kind": "execute",
        "rawInput": {"command": command},
    }
    update({"sessionUpdate": "tool_call", "status": "pending", **call})
    if command not in ALWAYS_ALLOWED:
        send(
            {
                "jsonrpc": "2.0",
                "id": PERMISSION_REQUEST_ID,
                "method": "session/request_permission",
                "params": {
                    "sessionId": SESSION_ID,
                    "toolCall": call,
                    "options": [
                        {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
                        {
                            "optionId": "allow-always",
                            "name": "Yes, always",
                            "kind": "allow_always",
                        },
                        {"optionId": "reject-once", "name": "No", "kind": "reject_once"},
                    ],
                },
            }
        )
        while True:
            reply = receive()
            if reply is None:
                return
            if reply.get("id") == PERMISSION_REQUEST_ID:
                break
        outcome = (reply.get("result") or {}).get("outcome") or {}
        chosen = outcome.get("optionId") if outcome.get("outcome") == "selected" else None
        if chosen not in ("allow-once", "allow-always"):
            update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Stopped, as asked."},
                }
            )
            respond(
                message_id,
                {"stopReason": "end_turn" if chosen else "cancelled"},
            )
            return
        if chosen == "allow-always":
            ALWAYS_ALLOWED.add(command)
    done = subprocess.run(
        command,
        shell=True,
        cwd=WORKING_DIRECTORIES.get(SESSION_ID) or None,
        capture_output=True,
        text=True,
    )
    update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_run",
            "status": "completed" if done.returncode == 0 else "failed",
            "content": [
                {
                    "type": "content",
                    "content": {"type": "text", "text": done.stdout + done.stderr},
                }
            ],
        }
    )
    update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "Ran it."},
        }
    )
    respond(message_id, {"stopReason": "end_turn"})


def ask_question(message_id: Any) -> None:
    """Ask the question, write down the answer, and say it was answered."""
    form = (CLIENT_CAPABILITIES.get("elicitation") or {}).get("form")
    if form is None:
        update(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Nobody to ask."},
            }
        )
        respond(message_id, {"stopReason": "end_turn"})
        return
    call = {
        "toolCallId": "call_ask",
        "title": "Which colour?",
        "kind": "other",
        "rawInput": {"questions": [{"question": "Which colour?"}]},
        "_meta": {"claudeCode": {"toolName": "AskUserQuestion"}},
    }
    update({"sessionUpdate": "tool_call", "status": "pending", **call})
    send(
        {
            "jsonrpc": "2.0",
            "id": ELICITATION_REQUEST_ID,
            "method": "elicitation/create",
            "params": {
                "sessionId": SESSION_ID,
                "toolCallId": "call_ask",
                "mode": "form",
                "message": "Which colour?",
                "requestedSchema": QUESTION_FORM,
            },
        }
    )
    while True:
        reply = receive()
        if reply is None:
            return
        if reply.get("id") == ELICITATION_REQUEST_ID:
            break
    answer = reply.get("result") or {}
    directory = Path(WORKING_DIRECTORIES.get(SESSION_ID) or ".")
    (directory / "answer.json").write_text(json.dumps(answer), encoding="utf-8")
    update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": f"Answered: {answer.get('action')}."},
        }
    )
    respond(message_id, {"stopReason": "end_turn"})


def run_turn(message_id: Any, options: set[str]) -> None:
    """One prompt turn: some updates, maybe a question, then a stop reason."""
    response_file = os.environ.get("FAKE_AGENT_RESPONSE_FILE")
    response = (
        Path(response_file).read_text(encoding="utf-8")
        if response_file
        else os.environ.get("FAKE_AGENT_RESPONSE", "Looking.")
    )
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
    if "--refuse-prompt" in options:
        # Said early and on the other pipe, the way a real adapter says it: the
        # complaint that explains a refusal is written while the turn is being
        # attempted, and the refusal itself arrives afterwards.
        print("fake-agent: upstream said 429: quota exhausted", file=sys.stderr)
        sys.stderr.flush()
    while True:
        message = receive()
        if message is None:
            return 0
        method = message.get("method")
        if method is None:
            continue  # An answer to something this agent asked.
        message_id = message.get("id")

        if method == "initialize":
            CLIENT_CAPABILITIES.update(
                (message.get("params") or {}).get("clientCapabilities") or {}
            )
            respond(message_id, capabilities(options))
        elif method == "session/new":
            if "--die-on-new-session" in options:
                if "--noisy" in options:
                    # More than the client will keep, so that which end it keeps
                    # is a choice this agent can make it demonstrate. Nineteen
                    # lines leaves room for the fatal one in a 20-line buffer.
                    for index in range(19):
                        print(f"codex-acp: {index} " + "noise " * 40, file=sys.stderr)
                print("codex-acp: everything is on fire", file=sys.stderr, flush=True)
                return 3
            if "--nameless-session" in options:
                respond(message_id, {})
            else:
                cwd = (message.get("params") or {}).get("cwd")
                if isinstance(cwd, str):
                    WORKING_DIRECTORIES[SESSION_ID] = cwd
                respond(message_id, {"sessionId": SESSION_ID})
        elif method == "session/set_config_option":
            if message.get("params", {}).get("value") == "unavailable":
                fail(message_id, -32602, "Unknown model")
            else:
                respond(message_id, {"configOptions": []})
        elif method == "session/load":
            update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "replayed history"},
                }
            )
            respond(message_id, None)
        elif method == "session/prompt":
            if "--rate-limit" in options:
                fail(
                    message_id,
                    -32603,
                    "Internal error: You've hit your session limit · resets 6pm (America/Denver)",
                    {"errorKind": "rate_limit"},
                )
            elif "--refuse-prompt" in options:
                # `data` carries the cause and a body the size real ones come
                # in -- an agent that puts an entire HTTP response in a refusal
                # is what the client's cap is for.
                fail(
                    message_id,
                    -32000,
                    "this session is over quota",
                    {"reason": "quota exhausted", "body": "x" * 4000},
                )
            elif "--ask" in options:
                ask_question(message_id)
            elif "--run-directive" in options:
                run_directive(message_id, message.get("params") or {})
            else:
                run_turn(message_id, options)
        elif method == "session/cancel":
            continue
        elif message_id is not None:
            fail(message_id, -32601, f"no such method: {method}")


if __name__ == "__main__":
    sys.exit(main())

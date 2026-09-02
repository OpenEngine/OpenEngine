"""A scripted ACP agent, for the tier that drives the graph runtime.

`tests/provider_fakes.py` is the same idea one protocol over: a real
subprocess, speaking a real wire protocol, running the commands it is really
allowed to run, with only what the model would have said replaced by a JSON
script. That one speaks Codex's app-server and Claude Code's stream-JSON,
because that is what `engine.adapters.agent_runner` drives. This one speaks
ACP, because that is what `engine.graph_runtime_langgraph` drives -- through
`langgraph-acp`, and through whichever adapter a provider is configured with.

The script is the *same file*, read from `ENGINE_FAKE_SCRIPT` and shaped the
same way, so a browser test can drive either backend from one `engine.script`
call:

    {"title": "...", "scenarios": [{"when": "greeting", "steps": [...]}]}

Three step types, matching the ones the other fake honours:

    say    an `agent_message_chunk` update
    run    `session/request_permission`, then really run the command
    tool   a `tool_call` update, for a call with no permission attached

`complete_step` has no counterpart here and needs none: a graph node ends when
its ACP turn ends, so there is no run-bound MCP server to call and nothing for
a `tool` step to reach. A script written for the other tier still runs -- its
terminal call becomes a tool update -- which is what makes the two tiers
comparable.

Sessions are kept on disk under `$ACP_FAKE_STATE`, because `session/load` has
to work in a process that did not create the session: that is the whole
approval-handoff story `engine.graph_runtime_langgraph.acp` tells, and an agent
that forgot on reload would let a broken handoff pass.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

#: Where sessions are kept, so `session/load` survives losing the process.
STATE_ENVIRONMENT_VARIABLE = "ACP_FAKE_STATE"

#: The ACP revision this agent claims. `langgraph-acp` sends its own and does
#: not negotiate down, so agreeing is the whole handshake.
PROTOCOL_VERSION = 1

#: The id every permission request is asked under. One outstanding request per
#: turn is all a script can produce, because a step blocks until it is answered.
PERMISSION_REQUEST_ID = 9001

#: What a refused command leaves the turn saying.
REFUSED = "Stopped: that was not allowed."


# --- installing one ---------------------------------------------------------


def install(name: str, directory: Path) -> str:
    """Write an executable named `name` into `directory`, and name its path.

    A shim for the same reason the other fake's is: the agent is this module,
    which is ordinary source somebody can read and edit, and a provider takes a
    command line rather than a class.
    """
    path = directory / name
    path.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(Path(__file__).resolve()))} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


# --- the script -------------------------------------------------------------


def _script() -> Mapping[str, Any]:
    path = os.environ.get("ENGINE_FAKE_SCRIPT")
    if not path:
        return {"scenarios": []}
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"could not read {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise SystemExit(f"{path} must hold a JSON object")
    return loaded


def _steps(prompt: str) -> Sequence[Mapping[str, Any]]:
    """The steps whose scenario this prompt selects. First match wins."""
    for scenario in _script().get("scenarios") or ():
        when = scenario.get("when")
        if when is None or str(when) in prompt:
            return list(scenario.get("steps") or ())
    return [{"type": "say", "text": "This turn was not scripted."}]


# --- sessions ---------------------------------------------------------------


def _state() -> Path:
    directory = Path(os.environ.get(STATE_ENVIRONMENT_VARIABLE, ".")).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _load(session_id: str) -> dict[str, Any]:
    path = _state() / f"{session_id}.json"
    if not path.exists():
        return {"cwd": os.getcwd(), "turns": [], "awaiting": None}
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _save(session_id: str, session: Mapping[str, Any]) -> None:
    (_state() / f"{session_id}.json").write_text(
        json.dumps(session), encoding="utf-8"
    )


# --- the transport ----------------------------------------------------------


def _send(message: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _receive() -> dict[str, Any] | None:
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        if line.strip():
            message: dict[str, Any] = json.loads(line)
            return message


def _respond(message_id: Any, result: Any) -> None:
    _send({"jsonrpc": "2.0", "id": message_id, "result": result})


def _update(session_id: str, payload: Mapping[str, Any]) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": payload},
        }
    )


def _say(session_id: str, text: str) -> None:
    _update(
        session_id,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        },
    )


def _tool(session_id: str, call_id: str, title: str, raw: Mapping[str, Any]) -> None:
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "title": title,
            "kind": "execute",
            "status": "in_progress",
            "rawInput": dict(raw),
        },
    )


def _tool_done(session_id: str, call_id: str, status: str) -> None:
    _update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": call_id,
            "status": status,
        },
    )


def _ask(session_id: str, call_id: str, title: str, command: str) -> bool:
    """Ask to run `command`, and block until the client answers or goes away."""
    _send(
        {
            "jsonrpc": "2.0",
            "id": PERMISSION_REQUEST_ID,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {
                    "toolCallId": call_id,
                    "title": title,
                    "kind": "execute",
                    "rawInput": {"command": command},
                },
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    while True:
        reply = _receive()
        if reply is None:
            raise SystemExit(0)  # The client went away mid-question.
        if reply.get("id") == PERMISSION_REQUEST_ID:
            outcome = (reply.get("result") or {}).get("outcome") or {}
            return (
                isinstance(outcome, dict)
                and outcome.get("outcome") == "selected"
                and str(outcome.get("optionId", "")).startswith("allow")
            )


# --- turns ------------------------------------------------------------------


def _prompt_text(params: Mapping[str, Any]) -> str:
    return "".join(
        str(block.get("text", ""))
        for block in params.get("prompt", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _run_turn(message_id: Any, session_id: str, prompt: str) -> None:
    session = _load(session_id)
    session["turns"].append(prompt)
    _save(session_id, session)
    cwd = str(session.get("cwd") or os.getcwd())
    for index, step in enumerate(_steps(prompt)):
        kind = str(step.get("type"))
        if kind == "say":
            _say(session_id, str(step.get("text", "")))
        elif kind == "run":
            command = str(step.get("command", ""))
            call_id = f"call-{index}"
            if step.get("approval", True) and not _ask(
                session_id, call_id, command, command
            ):
                _say(session_id, REFUSED)
                _respond(message_id, {"stopReason": "refusal"})
                return
            _tool(session_id, call_id, command, {"command": command})
            done = subprocess.run(
                command, shell=True, cwd=cwd, capture_output=True, text=True
            )
            _tool_done(
                session_id,
                call_id,
                "completed" if done.returncode == 0 else "failed",
            )
        elif kind == "tool":
            # No run-bound MCP server on this path: a graph node ends when its
            # turn does. Reported as a call so a script written for the other
            # tier still reads as having done something.
            call_id = f"call-{index}"
            _tool(
                session_id,
                call_id,
                str(step.get("name", "tool")),
                dict(step.get("arguments") or {}),
            )
            _tool_done(session_id, call_id, "completed")
    _respond(message_id, {"stopReason": "end_turn"})


def main() -> int:
    while True:
        message = _receive()
        if message is None:
            return 0
        method = message.get("method")
        if method is None:
            continue  # An answer to something this agent asked.
        message_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            _respond(
                message_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "agentCapabilities": {
                        "loadSession": True,
                        "promptCapabilities": {
                            "image": False,
                            "embeddedContext": False,
                        },
                    },
                },
            )
        elif method == "session/new":
            session_id = f"sess_{uuid.uuid4().hex[:8]}"
            _save(
                session_id,
                {"cwd": str(params.get("cwd") or os.getcwd()), "turns": []},
            )
            _respond(message_id, {"sessionId": session_id})
        elif method == "session/load":
            session_id = str(params.get("sessionId"))
            session = _load(session_id)
            if params.get("cwd"):
                session["cwd"] = str(params["cwd"])
            session["loads"] = int(session.get("loads", 0)) + 1
            _save(session_id, session)
            _respond(message_id, None)
        elif method == "session/prompt":
            _run_turn(
                message_id, str(params.get("sessionId")), _prompt_text(params)
            )
        elif method == "session/cancel":
            continue
        elif message_id is not None:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {"code": -32601, "message": f"no such method: {method}"},
                }
            )


if __name__ == "__main__":
    sys.exit(main())

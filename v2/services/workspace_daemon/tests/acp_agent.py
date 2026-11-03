"""Deterministic ACP agent subprocess for tests; needs no model, credentials or agent SDK.

    acp_agent.py LOG MODE

Every received frame is appended to LOG as JSON; known session IDs persist
beside it, so a restarted agent can load them. MODE selects one behavior:
`normal`, `no_resume`, `no_read_only`, `exit` (crash mid-prompt), `partial_exit`
(crash after streaming some text), `partial_hang` (stream some text, then hang), `hang` (never
answers the prompt), `stop_reading` (stops reading stdin after set_mode),
`flood` (many chunks), `child` (leaves a grandchild running), `cancellable`
(streams some text, leaves a grandchild running and ends the turn only on
`session/cancel`), `slow_start` (answers initialize after a second).
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

log, mode = Path(sys.argv[1]), sys.argv[2]
sessions = log.with_suffix(".sessions")


def record(value):
    with log.open("a") as file:
        file.write(json.dumps(value) + "\n")


def send(message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def chunk(session_id, text):
    send({"method": "session/update", "params": {"sessionId": session_id, "update": {
        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})


def receive():
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    value = json.loads(line)
    record(value)
    return value


record({"started": {"cwd": os.getcwd(), "environment": sorted(os.environ)}})
while True:
    request = receive()
    method, params, result = request.get("method"), request.get("params", {}), {}
    if method == "initialize":
        if mode == "slow_start":
            time.sleep(1)
        result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": mode != "no_resume"}}
    elif method in ("session/new", "session/load"):
        known = json.loads(sessions.read_text()) if sessions.exists() else []
        if method == "session/new":
            session_id = "acp_" + uuid.uuid4().hex
            sessions.write_text(json.dumps([*known, session_id]))
        elif params["sessionId"] not in known:
            send({"id": request["id"], "error": {"code": -32602, "message": "unknown session"}})
            continue
        else:
            session_id = params["sessionId"]
            chunk(session_id, "Replayed history")
        modes = [{"id": "auto", "name": "Auto"}] + ([] if mode == "no_read_only" else [{"id": "read-only", "name": "Read only"}])
        result = {"sessionId": session_id, "modes": {"currentModeId": "auto", "availableModes": modes},
                  "configOptions": [{"id": "model", "name": "Model", "category": "model", "type": "select",
                                     "currentValue": "fixture-small",
                                     "options": [{"value": "fixture-small", "name": "S"}, {"value": "fixture-large", "name": "L"}]}]}
    elif method == "session/prompt":
        session_id = params["sessionId"]
        if mode == "exit":
            sys.exit(3)
        if mode == "partial_exit":
            chunk(session_id, "Partial answer")
            sys.exit(3)
        if mode == "partial_hang":
            chunk(session_id, "Partial answer")
        if mode in ("hang", "partial_hang"):
            time.sleep(3600)
        if mode == "cancellable":
            chunk(session_id, "Partial answer")
            child = subprocess.Popen(["/bin/sleep", "300"])
            log.with_suffix(".child").write_text(str(child.pid))
            while receive().get("method") != "session/cancel":
                pass
            send({"id": request["id"], "result": {"stopReason": "cancelled"}})
            continue
        send({"id": "permission_1", "method": "session/request_permission", "params": {
            "sessionId": session_id, "toolCall": {"toolCallId": "call_1", "title": "Write a file"},
            "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]}})
        receive()
        send({"id": "fs_1", "method": "fs/write_text_file", "params": {"sessionId": session_id, "path": "/x", "content": "x"}})
        receive()
        if mode == "child":
            child = subprocess.Popen(["/bin/sleep", "300"])
            log.with_suffix(".child").write_text(str(child.pid))
        for index in range(40 if mode == "flood" else 0):
            chunk(session_id, f"{index:02d}" * 16000)  # one full 32,000-character message each
        chunk(session_id, "Inspected workspace. ")
        chunk(session_id, "Found the requested result.")
        # Cumulative context usage: not a delta, so never reported.
        send({"method": "session/update", "params": {"sessionId": session_id, "update": {
            "sessionUpdate": "usage_update", "used": 5000, "size": 10000}}})
        result = {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 7, "cachedReadTokens": 3,
                                                      "totalTokens": 20}}
    send({"id": request["id"], "result": result})
    if method == "session/set_mode" and mode == "stop_reading":
        time.sleep(3600)

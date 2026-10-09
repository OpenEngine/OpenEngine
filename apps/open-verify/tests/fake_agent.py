"""Deterministic ACP process fixture. No provider account or remote API required."""

import json
import sys
from pathlib import Path

decisions = iter(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))


def send(value):
    print(json.dumps({"jsonrpc": "2.0", **value}), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        result = {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}
    elif method == "session/new":
        result = {"sessionId": "qa-fixture"}
    elif method == "session/prompt":
        text = json.dumps(next(decisions))
        send(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "qa-fixture",
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                },
            }
        )
        result = {"stopReason": "end_turn"}
    elif request_id is None:
        continue
    else:
        result = {}
    send({"id": request_id, "result": result})

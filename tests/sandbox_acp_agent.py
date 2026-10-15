"""Small ACP peer that reports its actual sandbox environment and workspace."""

import json
import os
from pathlib import Path
import sys
import time
import uuid

session_id = uuid.uuid4().hex
cwd = None
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        if "--hang-init" in sys.argv:
            time.sleep(120)
        if "--exit-init" in sys.argv:
            print("agent initialization failed", file=sys.stderr, flush=True)
            sys.exit(7)
        result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
    elif method in {"session/new", "session/load"}:
        cwd = request["params"]["cwd"]
        assert Path(cwd).resolve() == Path.cwd()
        assert Path("input.txt").read_text() == "workspace-marker"
        result = {"sessionId": session_id}
    elif method == "session/prompt":
        data = {"cwd": cwd, "env": dict(os.environ), "marker": Path("input.txt").read_text()}
        if "--large" in sys.argv:
            data["large"] = "x" * 100_000
        if "--hang-prompt" in sys.argv:
            time.sleep(120)
        print(json.dumps({
            "jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": session_id, "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": json.dumps(data)},
            }},
        }), flush=True)
        result = {"stopReason": "end_turn"}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)

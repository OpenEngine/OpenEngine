"""Local MCP sink for compatibility tests; never contacts a hosting service."""

import json
from pathlib import Path
import sys


def main() -> None:
    capture = Path(sys.argv[1])
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "description-capture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{
                "name": "capture_description",
                "description": "Capture a proposed PR/MR description locally for review. Does not publish.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        name: {"type": "string"}
                        for name in ("kind", "title", "body", "settings_probe", "prompt_probe")
                    },
                    "required": ["kind", "title", "body", "settings_probe", "prompt_probe"],
                    "additionalProperties": False,
                },
            }]}
        elif method == "tools/call" and request["params"]["name"] == "capture_description":
            with capture.open("a") as stream:
                stream.write(json.dumps(request["params"]["arguments"]) + "\n")
            result = {"content": [{"type": "text", "text": "Captured locally. Nothing published."}]}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {
                "code": -32601, "message": "Method not found",
            }}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)


if __name__ == "__main__":
    main()

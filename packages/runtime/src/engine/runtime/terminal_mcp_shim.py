"""Standalone guest MCP stdio shim. Copied into tmpfs, never imports Engine."""

import json
from pathlib import Path
import socket
import sys

LIMIT = 8 * 1024 * 1024


def bounded_line(stream) -> bytes:
    line = stream.readline(LIMIT + 1)
    if len(line) > LIMIT:
        message = "MCP message exceeds byte budget"
        raise ValueError(message)
    return line


def main() -> None:
    config = json.loads((Path.home() / ".oe-mcp" / sys.argv[1]).read_text())
    while line := bounded_line(sys.stdin.buffer):
        try:
            request = json.loads(line)
        except ValueError:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Parse error"},
                    }
                ),
                flush=True,
            )
            continue
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(10)
                connection.connect(config["socket"])
                # Approvals can wait for human input; no response deadline.
                connection.settimeout(None)
                connection.sendall(
                    json.dumps({"token": config["token"], "request": request}).encode()
                    + b"\n"
                )
                with connection.makefile("rb") as stream:
                    raw = bounded_line(stream)
                response = json.loads(raw)
        except (OSError, ValueError):
            response = {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {
                    "code": -32603,
                    "message": "host terminal broker is unavailable",
                },
            }
        if isinstance(request, dict) and "id" not in request:
            continue
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()

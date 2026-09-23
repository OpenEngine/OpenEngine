"""Codex's app-server, with every turn held to the sandbox Engine chose.

codex-acp 1.13 does not let a client choose Codex's sandbox or approval policy.
It sends both with every `turn/start`, from one of three fixed presets, and none
of them is what Engine asks for: its `read-only` preset writes anywhere in the
worktree without asking, and its full-access preset never asks about anything.
Neither reaches the broker, so neither can be governed by it.

What codex-acp does let a client choose is the Codex it runs, through
`CODEX_PATH`. `codex_acp_runner` points that at this module, which starts the
real app-server and rewrites each `turn/start` on its way in: the sandbox policy
Engine named, `on-request` approval, and a person as the reviewer -- what the
app-server runner in `engine.adapters.agent_runner.codex` sends. Everything else
passes through untouched, and the app-server answers codex-acp directly.

The real Codex is `ENGINE_CODEX_PATH` when the operator named one, and otherwise
the one codex-acp would have run: `@openai/codex`, resolved from where the
`codex-acp` on `PATH` is installed, which is where `npx` puts it.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import IO, Any

#: The app-server sandbox policy for each Codex sandbox name Engine's settings use.
SANDBOX_POLICIES: Mapping[str, Mapping[str, Any]] = {
    "read-only": {"type": "readOnly"},
    "workspace-write": {"type": "workspaceWrite"},
    "danger-full-access": {"type": "dangerFullAccess"},
}

SANDBOX_VARIABLE = "ENGINE_CODEX_SANDBOX"
CODEX_PATH_VARIABLE = "ENGINE_CODEX_PATH"

#: Where `@openai/codex` keeps the script codex-acp runs when not told otherwise.
BUNDLED_CODEX = Path("node_modules", "@openai", "codex", "bin", "codex.js")


def pinned(message: dict[str, Any], sandbox: str) -> dict[str, Any]:
    """`message` with a `turn/start`'s policies replaced by Engine's."""
    params = message.get("params")
    if message.get("method") != "turn/start" or not isinstance(params, dict):
        return message
    return {
        **message,
        "params": {
            **params,
            "approvalPolicy": "on-request",
            "approvalsReviewer": "user",
            "sandboxPolicy": dict(SANDBOX_POLICIES[sandbox]),
        },
    }


def codex_command(environ: Mapping[str, str]) -> list[str]:
    """How to start the Codex codex-acp would otherwise have started itself."""
    named = environ.get(CODEX_PATH_VARIABLE)
    if named:
        return [named]
    adapter = shutil.which("codex-acp", path=environ.get("PATH"))
    node = shutil.which("node", path=environ.get("PATH"))
    if adapter and node:
        for directory in Path(adapter).resolve().parents:
            script = directory / BUNDLED_CODEX
            if script.is_file():
                return [node, str(script)]
    codex = shutil.which("codex", path=environ.get("PATH"))
    if codex:
        return [codex]
    raise SystemExit(
        "engine codex policy: cannot find the Codex codex-acp bundles; "
        f"set {CODEX_PATH_VARIABLE} to a codex binary"
    )


def _forward(source: IO[bytes], sink: IO[bytes], sandbox: str) -> None:
    try:
        for line in source:
            try:
                message = json.loads(line)
            except ValueError:
                sink.write(line)
            else:
                if isinstance(message, dict):
                    message = pinned(message, sandbox)
                sink.write(json.dumps(message).encode() + b"\n")
            sink.flush()
    except (BrokenPipeError, ValueError):
        pass
    finally:
        try:
            sink.close()
        except OSError:
            pass


def main() -> int:
    sandbox = os.environ.get(SANDBOX_VARIABLE, "")
    if sandbox not in SANDBOX_POLICIES:
        raise SystemExit(
            f"engine codex policy: {SANDBOX_VARIABLE} must be one of "
            f"{tuple(SANDBOX_POLICIES)}, got {sandbox!r}"
        )
    codex = subprocess.Popen(
        [*codex_command(os.environ), *sys.argv[1:]], stdin=subprocess.PIPE
    )
    assert codex.stdin is not None
    threading.Thread(
        target=_forward, args=(sys.stdin.buffer, codex.stdin, sandbox), daemon=True
    ).start()
    return codex.wait()


if __name__ == "__main__":
    sys.exit(main())

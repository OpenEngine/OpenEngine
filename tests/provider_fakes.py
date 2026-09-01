"""Fake `codex` and `claude` executables that speak the providers' protocols.

Not mocks of our adapters: real subprocesses, real newline-delimited JSON, a
real approval round trip, and a real `subprocess.run` of whatever command they
are allowed to run. What they do not have is a model, so what the agent
"decides" to do is scripted instead.

Two tiers drive them, which is why they live here rather than inside one test
module:

* `tests/test_cli_compatibility.py` runs the approval contract against them
  unscripted, taking the command from a `run:` directive in the prompt. The
  same three scenarios then run against the pinned real CLIs, and only a fake
  that speaks the real wire protocol makes that pair meaningful.
* `apps/web/e2e` drives a browser against a real server wired to them, with a
  script naming exactly what the agent says and does on the way through.

The script is JSON, read from `ENGINE_FAKE_SCRIPT` on every invocation, so a
test can change what the agent will do next without restarting the server it is
talking to:

    {"title": "Adding a greeting",
     "scenarios": [{"when": "greeting",
                    "steps": [{"type": "say", "text": "Reading the tree."},
                              {"type": "run", "command": "echo hi > hi.txt"},
                              {"type": "tool", "name": "complete_step",
                               "arguments": {"outcome": "success",
                                             "summary": "Added the greeting.",
                                             "outputs": {"pr_url": "..."}}}]}]}

`when` is matched as a substring of the prompt, and a scenario without one
matches anything -- so a conversation is scripted turn by turn by what each
turn is asked, rather than by a counter that a retry or a title would knock out
of step. The first matching scenario wins, so a reviewer's scenario -- whose
prompt quotes the task the implementation was given -- has to be listed before
the implementation's.

A `tool` step calls the run-bound MCP server the runtime attached to this
invocation, which is the only thing that ends a workflow step: an agent that
merely stops is corrected and asked again, and fails the run on the third pass.
The server is read off argv the way each provider encodes it and spawned as
given, credential and all, because the broker refuses a session it did not
issue.

Only turns that can pause are scripted. A turn run without the approval
transport is the runtime naming a chat or a workflow, and is answered with the
script's `title`: naming is not what any of these tests are about, and spending
a scenario on it would make every script carry one.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: What an unscripted run reads as the instruction to run one command. The
#: prompt is the only channel a live model and a fake share, so the
#: compatibility matrix drives both through it.
DIRECTIVE = "run:"

#: Where the script is, when there is one.
SCRIPT_ENVIRONMENT_VARIABLE = "ENGINE_FAKE_SCRIPT"

#: One command, taken from the prompt, gated on approval, then an answer. The
#: behaviour `test_cli_compatibility` expects from an unscripted fake.
DIRECTIVE_SCRIPT: Mapping[str, object] = {
    "scenarios": [{"steps": [{"type": "run"}, {"type": "say", "text": "Ran it."}]}]
}

#: What a non-interactive turn answers when the script does not name a title.
UNSCRIPTED_TITLE = "Scripted conversation"

#: The MCP revision this client speaks, which is the one the bound server does.
MCP_PROTOCOL_VERSION = "2025-06-18"

#: How long the MCP server gets to exit once its input has been closed. A call
#: itself is bounded by whatever is driving the turn, which has more to say
#: about a hang than this does.
MCP_TIMEOUT_SECONDS = 30.0

_THREAD_ID = "thread-1"
_TURN_ID = "turn-1"
_SESSION_ID = "session-1"


# --- installing one ---------------------------------------------------------


def install(provider: str, directory: Path) -> str:
    """Write an executable named `provider` into `directory`, and name its path.

    A shim rather than a generated program: the fake is this module, which is
    ordinary source that can be read, imported, and edited with the tools that
    edit source. The runners find their CLI with `shutil.which`, which accepts
    a path, so nothing has to be put on `PATH` for this to be the `codex` or
    `claude` a composed application runs.
    """

    path = directory / provider
    path.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(Path(__file__).resolve()))} {provider} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def fake_codex(directory: Path) -> str:
    """`codex exec` and `codex app-server`, as far as a scripted turn goes."""

    return install("codex", directory)


def fake_claude(directory: Path) -> str:
    """`claude -p`, with and without the bidirectional control protocol."""

    return install("claude", directory)


# --- reading the script -----------------------------------------------------


def _script() -> Mapping[str, object]:
    path = os.environ.get(SCRIPT_ENVIRONMENT_VARIABLE)
    if not path:
        return DIRECTIVE_SCRIPT
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"could not read {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise SystemExit(f"{path} must hold a JSON object")
    return loaded


def _title() -> str:
    title = _script().get("title")
    return str(title) if title else UNSCRIPTED_TITLE


def _steps(prompt: str) -> Sequence[Mapping[str, object]]:
    """The steps whose scenario this prompt selects."""

    scenarios = _script().get("scenarios") or ()
    if not isinstance(scenarios, list):
        raise SystemExit("scenarios must be a JSON array")
    for scenario in scenarios:
        when = scenario.get("when")
        if when is None or str(when) in prompt:
            return list(scenario.get("steps") or ())
    raise SystemExit("no scripted scenario matches this prompt")


def _command(step: Mapping[str, object], prompt: str) -> str:
    """What this step runs: what it says, or what the prompt directed."""

    command = step.get("command")
    if command is not None:
        return str(command)
    for line in reversed(prompt.splitlines()):
        # The last directive in a transcript is the newest instruction.
        if DIRECTIVE in line:
            return line.split(DIRECTIVE, 1)[1].strip()
    raise SystemExit(f"no {DIRECTIVE!r} directive in the prompt")


def _execute(command: str, cwd: str) -> tuple[int, str]:
    done = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True)
    return done.returncode, (done.stdout + done.stderr)


def _receive() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit("the client closed the transport mid-turn")
    return json.loads(line)


def _send(message: Mapping[str, object]) -> None:
    print(json.dumps(message), flush=True)


# --- calling the run-bound MCP server ---------------------------------------
#
# The runtime hands each provider the same three fields -- a name, a command,
# and its arguments -- and each encodes them its own way, so there is one reader
# per encoding and one client for all of them. No SDK: a fake whose whole point
# is that it speaks the wire protocol should speak this one by hand too.


@dataclass(frozen=True, slots=True)
class McpServer:
    """The stdio MCP server this invocation was told to talk to."""

    name: str
    command: str
    args: tuple[str, ...]


def _server_from_mapping(servers: object) -> McpServer | None:
    """One `{name: {command, args}}` entry, whichever transport carried it."""

    if not isinstance(servers, Mapping):
        return None
    for name, server in servers.items():
        if not isinstance(server, Mapping) or not server.get("command"):
            continue
        return McpServer(
            name=str(name),
            command=str(server["command"]),
            args=tuple(str(argument) for argument in server.get("args") or ()),
        )
    return None


def _claude_mcp_server(arguments: Sequence[str]) -> McpServer | None:
    """`--mcp-config '{"mcpServers": {"workflow": {...}}}'`, off argv."""

    for flag, value in zip(arguments, arguments[1:]):
        if flag != "--mcp-config":
            continue
        try:
            configured = json.loads(value)
        except json.JSONDecodeError as error:
            raise SystemExit(f"--mcp-config is not valid JSON: {error}") from error
        server = _server_from_mapping(
            configured.get("mcpServers") if isinstance(configured, dict) else None
        )
        if server is not None:
            return server
    return None


def _codex_mcp_server(arguments: Sequence[str]) -> McpServer | None:
    """`-c mcp_servers.<name>.command=<json>` and its `.args` sibling, off argv."""

    servers: dict[str, dict[str, object]] = {}
    for flag, value in zip(arguments, arguments[1:]):
        if flag != "-c" or not value.startswith("mcp_servers."):
            continue
        setting, _, encoded = value.partition("=")
        parts = setting.split(".")
        if len(parts) != 3:
            continue
        try:
            servers.setdefault(parts[1], {})[parts[2]] = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise SystemExit(f"{setting} is not valid JSON: {error}") from error
    return _server_from_mapping(servers)


def _codex_thread_mcp_server(params: object) -> McpServer | None:
    """The same three fields, as `thread/start` carries them on app-server."""

    config = params.get("config") if isinstance(params, Mapping) else None
    return _server_from_mapping(
        config.get("mcp_servers") if isinstance(config, Mapping) else None
    )


def _require(server: McpServer | None, provider: str) -> McpServer:
    if server is None:
        raise SystemExit(
            f"this {provider} turn was given no MCP server, so a 'tool' step "
            "has nothing to call"
        )
    return server


def _is_loopback_broker(server: McpServer) -> bool:
    """Whether this MCP process has to connect back to Engine over TCP."""

    return "engine.runtime.planning_mcp_server" in server.args


def _call_tool(
    server: McpServer, name: str, arguments: object
) -> tuple[str, bool]:
    """Call one tool over stdio MCP, and report what the server answered.

    The command is spawned exactly as it was handed over, credential included:
    the token is issued per agent run and the broker refuses a session it did
    not issue, so a reconstructed argv would be turned away.
    """

    process = subprocess.Popen(
        [server.command, *server.args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        _mcp_request(
            process,
            1,
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "provider-fake", "version": "1"},
            },
        )
        _mcp_notify(process, "notifications/initialized")
        result = _mcp_request(
            process, 2, "tools/call", {"name": name, "arguments": arguments}
        )
    finally:
        _close(process)
    text = "\n".join(
        str(block.get("text", ""))
        for block in result.get("content") or ()
        if isinstance(block, Mapping)
    )
    return text or "(no content)", bool(result.get("isError"))


def _mcp_request(
    process: "subprocess.Popen[str]",
    request_id: int,
    method: str,
    params: Mapping[str, object],
) -> Mapping[str, object]:
    _mcp_write(process, {"id": request_id, "method": method, "params": params})
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        if not line:
            raise SystemExit(f"the MCP server closed before answering {method}")
        message = json.loads(line)
        if message.get("id") != request_id:
            continue
        if message.get("error"):
            raise SystemExit(f"MCP {method} failed: {message['error']}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}


def _mcp_notify(process: "subprocess.Popen[str]", method: str) -> None:
    """A notification carries no id, and is answered by nothing."""

    _mcp_write(process, {"method": method, "params": {}})


def _mcp_write(
    process: "subprocess.Popen[str]", message: Mapping[str, object]
) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    process.stdin.flush()


def _close(process: "subprocess.Popen[str]") -> None:
    """Let the server end on its own closed input, and insist if it does not."""

    if process.stdin is not None:
        process.stdin.close()
    try:
        process.wait(timeout=MCP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


# --- codex ------------------------------------------------------------------


def _codex(arguments: Sequence[str]) -> int:
    if "app-server" in arguments:
        return _codex_app_server(arguments)
    return _codex_exec()


def _codex_exec() -> int:
    """`codex exec --json`: one turn, start to finish, prompt on stdin."""

    sys.stdin.read()
    _send({"type": "thread.started", "thread_id": _THREAD_ID})
    _send({"type": "turn.started"})
    _send(
        {
            "type": "item.completed",
            "item": {"id": "msg-1", "type": "agent_message", "text": _title()},
        }
    )
    _send({"type": "turn.completed", "usage": {}})
    return 0


def _codex_app_server(arguments: Sequence[str]) -> int:
    """The stdio JSON-RPC transport an approval-bearing turn is driven over."""

    initialize = _receive()
    _send({"id": initialize["id"], "result": {"userAgent": "fake-codex"}})
    assert _receive()["method"] == "initialized"

    start = _receive()
    assert start["method"] == "thread/start"
    # App-server takes its MCP servers in the thread's config rather than on
    # argv, which is where `codex exec` takes them. Either may carry one.
    server = _codex_thread_mcp_server(start.get("params")) or _codex_mcp_server(
        arguments
    )
    _send({"id": start["id"], "result": {"thread": {"id": _THREAD_ID}}})

    turn = _receive()
    assert turn["method"] == "turn/start"
    params = turn["params"]
    cwd = params.get("cwd") or os.getcwd()
    prompt = params["input"][0]["text"]
    _send({"id": turn["id"], "result": {"turn": {"id": _TURN_ID}}})

    for index, step in enumerate(_steps(prompt), start=1):
        kind = step.get("type")
        if kind == "say":
            _codex_item(
                "item/completed",
                {"id": f"msg-{index}", "type": "agentMessage", "text": step["text"]},
            )
            continue
        if kind == "tool":
            called = _require(server, "codex")
            name = str(step["name"])
            call_arguments = step.get("arguments") or {}
            call = {
                "id": f"tool-{index}",
                "type": "mcpToolCall",
                "server": called.name,
                "tool": name,
                "arguments": json.dumps(call_arguments),
                "status": "inProgress",
            }
            _codex_item("item/started", call)
            sandbox = params.get("sandboxPolicy") or {}
            if _is_loopback_broker(called) and not sandbox.get("networkAccess"):
                _codex_item("item/completed", {**call, "status": "cancelled"})
                continue
            output, failed = _call_tool(called, name, call_arguments)
            _codex_item(
                "item/completed",
                {
                    **call,
                    "status": "failed" if failed else "completed",
                    "result": output,
                },
            )
            continue
        if kind != "run":
            raise SystemExit(f"codex cannot play a {kind!r} step")

        command = _command(step, prompt)
        item = {
            "id": f"cmd-{index}",
            "type": "commandExecution",
            "command": command,
            "cwd": cwd,
            "commandActions": [],
            "status": "inProgress",
        }
        _codex_item("item/started", item)
        if step.get("approval", True):
            _send(
                {
                    "id": f"approval-{index}",
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": _THREAD_ID,
                        "turnId": _TURN_ID,
                        "itemId": item["id"],
                        "reason": "the command would write outside the sandbox",
                        "command": command,
                        "cwd": cwd,
                        "commandActions": [],
                        "availableDecisions": [
                            "accept",
                            "acceptForSession",
                            "decline",
                            "cancel",
                        ],
                    },
                }
            )
            decision = _receive()["result"]["decision"]
            if decision in {"decline", "cancel"}:
                # What the real app-server does with a refused command: the turn
                # ends, and the command never runs.
                _send(
                    {
                        "method": "turn/completed",
                        "params": {
                            "threadId": _THREAD_ID,
                            "turn": {"id": _TURN_ID, "items": [], "status": "interrupted"},
                        },
                    }
                )
                return 0

        exit_code, output = _execute(command, cwd)
        _codex_item(
            "item/completed",
            {
                **item,
                "status": "completed",
                "exitCode": exit_code,
                "aggregatedOutput": output,
            },
        )

    _send(
        {
            "method": "thread/tokenUsage/updated",
            "params": {
                "threadId": _THREAD_ID,
                "turnId": _TURN_ID,
                "tokenUsage": {
                    "last": {"inputTokens": 10, "cachedInputTokens": 4, "outputTokens": 2},
                    "total": {"inputTokens": 10, "cachedInputTokens": 4, "outputTokens": 2},
                },
            },
        }
    )
    _send(
        {
            "method": "turn/completed",
            "params": {
                "threadId": _THREAD_ID,
                "turn": {"id": _TURN_ID, "items": [], "status": "completed"},
            },
        }
    )
    return 0


def _codex_item(method: str, item: Mapping[str, object]) -> None:
    _send(
        {
            "method": method,
            "params": {"threadId": _THREAD_ID, "turnId": _TURN_ID, "item": item},
        }
    )


# --- claude code ------------------------------------------------------------


def _claude(arguments: Sequence[str]) -> int:
    if "--input-format" in arguments:
        return _claude_interactive(arguments)
    return _claude_print()


def _claude_print() -> int:
    """`claude -p --output-format stream-json`: one turn, prompt on stdin."""

    sys.stdin.read()
    title = _title()
    _send({"type": "system", "subtype": "init", "session_id": _SESSION_ID})
    _send({"type": "assistant", "message": {"content": [{"type": "text", "text": title}]}})
    _send(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": title,
            "usage": {},
        }
    )
    return 0


def _claude_interactive(arguments: Sequence[str]) -> int:
    """The same, plus stream-JSON input and the permission control protocol."""

    server = _claude_mcp_server(arguments)
    initialize = _receive()
    _send(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": initialize["request_id"],
                "response": {"commands": []},
            },
        }
    )

    user = _receive()
    assert user["type"] == "user"
    prompt = user["message"]["content"]
    cwd = os.getcwd()
    _send({"type": "system", "subtype": "init", "session_id": _SESSION_ID})

    answer = ""
    for index, step in enumerate(_steps(prompt), start=1):
        kind = step.get("type")
        if kind == "say":
            answer = str(step["text"])
            _send(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": answer}]},
                }
            )
            continue
        if kind == "tool":
            called = _require(server, "claude")
            name = str(step["name"])
            call_arguments = step.get("arguments") or {}
            tool_use_id = f"toolu_{index}"
            # Claude names an MCP tool `mcp__<server>__<tool>`, which is also
            # the name it has to have been allowed under to be callable at all.
            _send(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tool_use_id,
                                "name": f"mcp__{called.name}__{name}",
                                "input": call_arguments,
                            }
                        ]
                    },
                }
            )
            output, failed = _call_tool(called, name, call_arguments)
            _send(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_use_id,
                                "content": output,
                                "is_error": failed,
                            }
                        ]
                    },
                }
            )
            continue
        if kind != "run":
            raise SystemExit(f"claude cannot play a {kind!r} step")

        command = _command(step, prompt)
        tool_use_id = f"toolu_{index}"
        _send(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ]
                },
            }
        )
        if step.get("approval", True):
            _send(
                {
                    "type": "control_request",
                    "request_id": f"permission-{index}",
                    "request": {
                        "subtype": "can_use_tool",
                        "tool_name": "Bash",
                        "input": {"command": command},
                        "tool_use_id": tool_use_id,
                        "title": "Claude wants to run a command",
                        "permission_suggestions": [
                            {
                                "type": "addRules",
                                "rules": [
                                    {"toolName": "Bash", "ruleContent": command}
                                ],
                                "behavior": "allow",
                                "destination": "localSettings",
                            }
                        ],
                    },
                }
            )
            response = _receive()["response"]["response"]
            if response["behavior"] == "deny":
                # deny + interrupt ends the turn, and the command never runs.
                _send(
                    {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "result": response.get("message", "denied"),
                        "usage": {},
                    }
                )
                return 0

        exit_code, output = _execute(command, cwd)
        _send(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": output or "(no output)",
                            "is_error": exit_code != 0,
                        }
                    ]
                },
            }
        )

    _send(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": answer or "Done.",
            "usage": {},
        }
    )
    return 0


PROVIDERS = {"codex": _codex, "claude": _claude}


def main(argv: Sequence[str]) -> int:
    if not argv or argv[0] not in PROVIDERS:
        raise SystemExit(f"usage: {Path(__file__).name} {'|'.join(PROVIDERS)} [...]")
    return PROVIDERS[argv[0]](tuple(argv[1:]))


__all__ = [
    "DIRECTIVE",
    "DIRECTIVE_SCRIPT",
    "SCRIPT_ENVIRONMENT_VARIABLE",
    "UNSCRIPTED_TITLE",
    "fake_claude",
    "fake_codex",
    "install",
]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

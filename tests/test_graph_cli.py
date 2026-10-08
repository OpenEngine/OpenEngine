"""`engine backend|graph|loop|node` from the terminal side.

The backend is replaced by a recording `urlopen`, because what is under test
here is the client: which backend it picks, what it sends, and that a write is
retried only under the idempotency key that makes the retry safe.
"""

import io
import json
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from engine.apps.cli.__main__ import main
from engine.cli import backends, http


class Backend:
    """Answers each request from a queue, recording what was sent."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float = 0) -> Any:
        self.requests.append({
            "method": request.get_method(),
            "url": request.full_url,
            "body": json.loads(request.data) if request.data else None,
            "authorization": request.get_header("Authorization"),
        })
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return _Response(answer)


class _Response(io.BytesIO):
    def __init__(self, payload: Any) -> None:
        super().__init__(json.dumps(payload).encode())

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(backends.FILE_ENVIRONMENT_VARIABLE, str(tmp_path / "backends.json"))
    monkeypatch.setenv("ENGINE_CLI_CONFIG", str(tmp_path / "cli.json"))
    monkeypatch.delenv(backends.SELECTED_ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.delenv("ENGINE_SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(http.time, "sleep", lambda _seconds: None)


def serve(monkeypatch, *answers: Any) -> Backend:
    backend = Backend(*answers)
    monkeypatch.setattr(http, "urlopen", backend)
    return backend


RUN = {
    "runId": "run-1", "graph": "pair", "version": 1, "status": "running", "terminal": False,
    "nodes": [], "results": {}, "failure": None, "usage": {"costUsd": None}, "pullRequests": [],
}


def test_backends_are_added_selected_and_listed(capsys) -> None:
    assert main(["backend", "add", "mini", "mac-mini.local:4364", "--token-env", "MINI_TOKEN", "--use"]) == 0
    capsys.readouterr()
    assert main(["backends", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["current"] == "mini"
    assert {b["name"]: b["url"] for b in listed["backends"]} == {
        "local": "http://127.0.0.1:4364", "mini": "http://mac-mini.local:4364",
    }
    assert main(["backend", "use", "local"]) == 0
    assert backends.load().current == "local"
    assert main(["backend", "add", "mini", "http://other:1"]) == 1
    assert "already exists" in capsys.readouterr().err
    assert main(["backend", "remove", "mini"]) == 0
    assert list(backends.load().backends) == []


def test_a_token_is_named_never_stored() -> None:
    with pytest.raises(backends.BackendError, match="names an environment variable"):
        backends.add("mini", "http://mini:4364", token_env="ghp-secret-value")


def test_every_command_goes_to_the_selected_backend_with_its_token(monkeypatch, capsys) -> None:
    backends.add("mini", "http://mini:4364", token_env="MINI_TOKEN", project="team", use=True)
    monkeypatch.setenv("MINI_TOKEN", "secret")
    recorded = serve(monkeypatch, {"graphs": []}, {"graphs": []})
    assert main(["graphs", "list"]) == 0
    assert main(["graphs", "list", "--backend", "local"]) == 0
    first, second = recorded.requests
    assert first["url"] == "http://mini:4364/api/v1/graphs?project=team"
    assert first["authorization"] == "Bearer secret"
    assert second["url"].startswith("http://127.0.0.1:4364/api/v1/graphs")


def test_execute_retries_a_dropped_connection_under_the_same_key(monkeypatch, capsys) -> None:
    recorded = serve(monkeypatch, URLError("connection reset"), {**RUN, "created": True})
    assert main(["graph", "execute", "pair", "fix the flaky test", "-i", "tone=terse", "--repo", "o/r"]) == 0
    first, retry = recorded.requests
    assert first["body"] == retry["body"]
    assert first["body"]["idempotencyKey"].startswith("cli-")
    assert first["body"]["inputs"] == {"tone": "terse"}
    assert json.loads(capsys.readouterr().out)["runId"] == "run-1"


def test_a_failed_run_names_the_signin_command_for_its_backend(monkeypatch, capsys) -> None:
    backends.add("mini", "http://mini:4364", use=True)
    failed = {
        **RUN, "status": "failed", "terminal": True,
        "failure": {"error": "Authentication required", "node": "work", "authRequired": {
            "runner": "claude", "command": "engine agent signin claude", "message": "",
        }},
    }
    serve(monkeypatch, failed)
    assert main(["run", "get", "run-1"]) == 1
    assert "engine agent signin claude --backend mini" in capsys.readouterr().err


def test_an_invalid_manifest_lists_each_problem(tmp_path, monkeypatch, capsys) -> None:
    manifest = tmp_path / "graph.yaml"
    manifest.write_text("name: x\nnodes: []\n")
    refusal = HTTPError("http://x", 400, "Bad Request", {}, io.BytesIO(json.dumps({
        "error": "the graph manifest is invalid",
        "problems": [{"path": "nodes", "message": "required; a list of at least one node"}],
    }).encode()))
    recorded = serve(monkeypatch, refusal)
    assert main(["graph", "add", str(manifest)]) == 1
    assert recorded.requests[0]["body"] | {"project": None} == {
        "project": None, "format": "yaml", "source": "name: x\nnodes: []\n",
    }
    error = capsys.readouterr().err
    assert "nodes: required" in error and "manifest is invalid" in error


def test_steering_sends_a_key_and_reports_a_refusal(monkeypatch, capsys) -> None:
    refusal = HTTPError("http://x", 409, "Conflict", {}, io.BytesIO(json.dumps({
        "error": "node execution e1 (work, attempt 1) is completed; only a running attempt can be steered",
    }).encode()))
    recorded = serve(monkeypatch, refusal)
    assert main(["node", "steer", "e1", "use the other approach"]) == 1
    assert recorded.requests[0]["body"]["idempotencyKey"].startswith("cli-")
    assert len(recorded.requests) == 1  # a refusal is an answer, not a dropped connection
    assert "only a running attempt" in capsys.readouterr().err


def test_loop_add_sends_limits_and_explains_what_spend_counts(monkeypatch, capsys) -> None:
    loop = {
        "loopId": "loop-1", "name": "pair", "state": "active", "pauseReason": None,
        "limits": {"maxPrs": 5, "maxSpendUsd": 20.0, "spendScope": "Agent usage reported by each node."},
    }
    recorded = serve(monkeypatch, loop)
    assert main(["loop", "add", "pair", "--max-prs", "5", "--max-spend", "20", "--every", "6h"]) == 0
    body = recorded.requests[0]["body"]
    assert (body["maxPrs"], body["maxSpendUsd"], body["every"], body["startNow"]) == (5, 20.0, "6h", True)
    assert "max-spend counts agent usage" in capsys.readouterr().err


def test_signing_in_on_a_remote_backend_says_where(monkeypatch, capsys) -> None:
    backends.add("mini", "http://mac-mini.local:4364", use=True)
    assert main(["agent", "signin", "codex"]) == 0
    out = capsys.readouterr().out
    assert "codex login" in out and "ssh -t mac-mini.local codex login" in out


def test_runner_signin_still_works_and_names_its_replacement(monkeypatch, capsys) -> None:
    backends.add("mini", "http://mac-mini.local:4364", use=True)
    assert main(["runner", "signin", "codex"]) == 0
    captured = capsys.readouterr()
    assert "ssh -t mac-mini.local codex login" in captured.out
    assert "engine agent signin codex" in captured.err


def test_an_added_agent_signs_in_as_its_kind(monkeypatch, capsys) -> None:
    backends.add("mini", "http://mac-mini.local:4364", use=True)
    backend = serve(monkeypatch, {"name": "reviewer", "kind": "claude", "model": "opus", "url": "", "builtin": False})
    assert main(["agent", "signin", "reviewer"]) == 0
    assert backend.requests[0]["url"] == "http://mac-mini.local:4364/api/v1/agents/reviewer"
    assert "ssh -t mac-mini.local claude" in capsys.readouterr().out


def test_an_agent_on_a_model_server_has_nothing_to_sign_in_to(monkeypatch, capsys) -> None:
    serve(monkeypatch, {"name": "qwen", "kind": "opencode", "model": "q", "url": "http://gpu:8000/v1", "builtin": False})
    assert main(["agent", "signin", "qwen"]) == 0
    assert "nothing to sign in to" in capsys.readouterr().out


def test_agents_are_added_listed_and_removed_on_the_backend(monkeypatch, capsys) -> None:
    qwen = {"name": "qwen", "kind": "opencode", "model": "qwen3", "url": "http://gpu:8000/v1", "builtin": False}
    backend = serve(
        monkeypatch, qwen,
        {"agents": [{"name": "claude", "kind": "claude", "model": "", "url": "", "builtin": True}, qwen]},
        {**qwen, "removed": True},
    )
    assert main(["agent", "add", "opencode", "--name", "qwen", "--model", "qwen3", "--url", "http://gpu:8000/v1"]) == 0
    assert main(["agents", "--pretty"]) == 0
    assert main(["agent", "remove", "qwen", "--pretty"]) == 0
    assert [(r["method"], r["url"].removeprefix("http://127.0.0.1:4364/api/v1")) for r in backend.requests] == [
        ("POST", "/agents"), ("GET", "/agents"), ("DELETE", "/agents/qwen"),
    ]
    assert backend.requests[0]["body"] == {
        "kind": "opencode", "name": "qwen", "model": "qwen3", "url": "http://gpu:8000/v1", "replace": False,
    }
    out = capsys.readouterr().out
    assert "built in" in out and "http://gpu:8000/v1" in out and "removed qwen" in out


def test_execute_sends_the_repository_it_is_run_from(tmp_path, monkeypatch) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    recorded = serve(monkeypatch, RUN)
    assert main(["graph", "execute", "pair", "say hello"]) == 0
    assert recorded.requests[0]["body"]["repository"] == str(tmp_path.resolve())

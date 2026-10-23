"""`oe backend|graph|loop|node` from the terminal side.

The backend is replaced by a recording `urlopen`, because what is under test
here is the client: which backend it picks, what it sends, and that a write is
retried only under the idempotency key that makes the retry safe.
"""

import io
import json
from pathlib import Path
from runpy import run_path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest
from engine.apps.cli.__main__ import main
from engine.cli import backends, http, starters


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
    monkeypatch.setattr("engine.cli.commands._check_trust", lambda *args: None)
    monkeypatch.setenv(backends.FILE_ENVIRONMENT_VARIABLE, str(tmp_path / "backends.json"))
    monkeypatch.setenv("ENGINE_CLI_CONFIG", str(tmp_path / "cli.json"))
    monkeypatch.delenv(backends.SELECTED_ENVIRONMENT_VARIABLE, raising=False)
    monkeypatch.delenv("ENGINE_SERVICE_TOKEN", raising=False)
    monkeypatch.setattr(http.time, "sleep", lambda _seconds: None)


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def generator() -> dict[str, Any]:
    return run_path(str(ROOT / "scripts/generate_cli_docs.py"))


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
    assert capsys.readouterr().out == "* mini  http://mac-mini.local:4364  project default\n"
    assert main(["backend", "use", "mini", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "mini"
    assert main(["backends", "list", "--json"]) == 0
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


def test_graph_run_retries_a_dropped_connection_under_the_same_key(monkeypatch, capsys) -> None:
    recorded = serve(monkeypatch, URLError("connection reset"), {**RUN, "created": True})
    assert main(["graph", "run", "pair", "fix the flaky test", "-i", "tone=terse", "--repo", "o/r", "--json"]) == 0
    first, retry = recorded.requests
    assert first["body"] == retry["body"]
    assert first["body"]["idempotencyKey"].startswith("cli-")
    assert first["body"]["inputs"] == {"tone": "terse"}
    assert json.loads(capsys.readouterr().out)["runId"] == "run-1"


def test_a_built_in_graph_is_registered_the_first_time_it_is_run(monkeypatch, capsys) -> None:
    missing = HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(json.dumps({
        "error": "no graph named 'adversarial-review' in project 'default'",
    }).encode()))
    registered = {"name": "adversarial-review", "version": 1, "created": True}
    recorded = serve(monkeypatch, missing, registered, {**RUN, "created": True})
    assert main([
        "graph", "run", "adversarial-review", "--branch", "feat/my_feat", "--agent", "codex", "--repo", "o/r",
    ]) == 0
    first, adding, retry = recorded.requests
    assert first["method"] == "GET"
    assert retry["body"]["inputs"] == {"adversarial": "true",
                                               "ref": "feat/my_feat", "implementation_runner": "codex",
                                               "review_runner": "codex"}
    assert retry["body"]["graph"] == "review"
    assert "deprecated" in capsys.readouterr().err
    assert retry["body"]["instruction"] == "Run adversarial-review on feat/my_feat."
    assert adding["url"].endswith("/api/v1/graphs")
    assert adding["body"]["source"] == starters.source("adversarial-review")


def test_an_unknown_graph_is_not_registered(monkeypatch, capsys) -> None:
    missing = HTTPError("http://x", 404, "Not Found", {}, io.BytesIO(json.dumps({
        "error": "no graph named 'mine'",
    }).encode()))
    recorded = serve(monkeypatch, missing)
    assert main(["graph", "run", "mine", "go", "--repo", "o/r"]) == 1
    assert len(recorded.requests) == 1


def test_runs_sends_its_filters_and_prints_a_table(monkeypatch, capsys) -> None:
    listed = {**RUN, "loop": None, "startedAt": "2026-10-05T12:00:00+00:00",
              "usage": {"costUsd": 1.25, "complete": True}}
    recorded = serve(monkeypatch, {"runs": [listed]}, {"runs": [listed]})
    assert main(["runs", "--graph", "pair", "--status", "running", "--limit", "5", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["runs"][0]["runId"] == "run-1"
    assert main(["runs", "--all-projects"]) == 0
    filtered, everywhere = recorded.requests
    assert filtered["url"].endswith("/api/v1/runs?project=default&graph=pair&status=running&limit=5")
    assert everywhere["url"].endswith("/api/v1/runs?limit=20")
    table = capsys.readouterr().out
    assert "RUN ID" in table and "pair v1" in table and "$1.25" in table


def test_a_failed_run_names_the_signin_command_for_its_backend(monkeypatch, capsys) -> None:
    backends.add("mini", "http://mini:4364", use=True)
    failed = {
        **RUN, "status": "failed", "terminal": True,
        "failure": {"error": "Authentication required", "node": "work", "authRequired": {
            "runner": "claude", "command": "oe agent signin claude", "message": "",
        }},
    }
    serve(monkeypatch, failed)
    assert main(["run", "get", "run-1"]) == 1
    assert "oe agent signin claude --backend mini" in capsys.readouterr().err


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
    assert main(["loop", "add", "pair", "--max-prs", "5", "--max-spend", "20", "--every", "6h", "--json"]) == 0
    body = recorded.requests[0]["body"]
    assert (body["maxPrs"], body["maxSpendUsd"], body["every"], body["startNow"]) == (5, 20.0, "6h", True)
    assert "max-spend counts agent usage" in capsys.readouterr().err


def test_loop_get_prints_the_latest_output(monkeypatch, capsys) -> None:
    loop = {
        "loopId": "loop-1", "name": "dead-code", "state": "active", "pauseReason": None,
        "graph": "dead-code", "version": 5, "everySeconds": 86400, "nextRunAt": "2026-10-10T18:00:00+00:00",
        "activeRunId": None, "prCount": 0, "spend": {"usd": 0.0, "complete": True},
        "limits": {"maxPrs": None, "maxSpendUsd": None},
        "latestOutput": {
            "runId": "run-1", "node": "challenge", "finishedAt": "2026-10-09T18:26:25+00:00",
            "value": {"summary": "One candidate survived.", "runner": "codex",
                      "report": "# Dead code\n\n- packages/x.py:3 unused helper"},
        },
    }
    serve(monkeypatch, loop)
    assert main(["loop", "get", "dead-code"]) == 0
    out = capsys.readouterr().out
    assert "latest output: challenge of run-1, finished 2026-10-09T18:26:25+00:00" in out
    assert "summary: One candidate survived." in out
    assert "report:\n# Dead code\n\n- packages/x.py:3 unused helper" in out
    assert "codex" not in out


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
    assert "oe agent signin codex" in captured.err


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


def test_agents_show_each_runners_binary_and_tier_models(monkeypatch, capsys) -> None:
    codex = {
        "name": "codex", "kind": "codex", "model": "", "url": "", "builtin": True,
        "path": "/opt/bin/codex", "version": "codex-cli 0.200.0",
        "models": {"cheapest": "", "default": "codex-main", "elevated": ""},
    }
    claude = {
        "name": "claude", "kind": "claude", "model": "", "url": "", "builtin": True, "path": "", "version": "",
        "models": {"cheapest": "haiku", "default": "sonnet", "elevated": "opus"},
    }
    serve(monkeypatch, {"agents": [codex, claude]}, codex)
    assert main(["agents"]) == 0
    out = capsys.readouterr().out
    assert "codex  /opt/bin/codex (codex-cli 0.200.0)\n" in out
    assert "  cheapest  (agent default)\n  default   codex-main\n  elevated  (agent default)\n" in out
    assert "claude  the adapter's bundled binary\n  cheapest  haiku\n  default   sonnet\n  elevated  opus" in out
    assert main(["agent", "get", "codex"]) == 0
    assert "  default   codex-main" in capsys.readouterr().out


def test_agents_are_added_listed_and_removed_on_the_backend(monkeypatch, capsys) -> None:
    qwen = {
        "name": "qwen", "kind": "opencode", "model": "qwen3", "url": "http://gpu:8000/v1",
        "contextLimit": 65536, "outputLimit": None, "builtin": False,
    }
    backend = serve(
        monkeypatch, qwen,
        {"agents": [{"name": "claude", "kind": "claude", "model": "", "url": "", "builtin": True}, qwen]},
        {**qwen, "removed": True},
    )
    assert main([
        "agent", "add", "opencode", "--name", "qwen", "--model", "qwen3", "--url", "http://gpu:8000/v1",
        "--context", "65536",
    ]) == 0
    assert main(["agents"]) == 0
    assert main(["agent", "remove", "qwen"]) == 0
    assert [(r["method"], r["url"].removeprefix("http://127.0.0.1:4364/api/v1")) for r in backend.requests] == [
        ("POST", "/agents"), ("GET", "/agents"), ("DELETE", "/agents/qwen"),
    ]
    assert backend.requests[0]["body"] == {
        "kind": "opencode", "name": "qwen", "model": "qwen3", "url": "http://gpu:8000/v1",
        "contextLimit": 65536, "outputLimit": None, "replace": False,
    }
    out = capsys.readouterr().out
    assert "built in" in out and "http://gpu:8000/v1" in out and "removed qwen" in out
    assert "LIMIT" in out and "65536" in out


def test_graph_run_sends_the_repository_it_is_run_from(
    tmp_path, monkeypatch, *, git_repo
) -> None:
    git_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    recorded = serve(monkeypatch, RUN)
    assert main(["graph", "run", "pair", "say hello"]) == 0
    assert recorded.requests[0]["body"]["repository"] == str(tmp_path.resolve())


@pytest.mark.parametrize("command", ["graph", "loop"])
def test_spec_prints_current_documentation_without_a_backend(command, monkeypatch, capsys) -> None:
    from inspect import getdoc
    from engine.cli import specs

    recorded = serve(monkeypatch)
    monkeypatch.setenv(backends.SELECTED_ENVIRONMENT_VARIABLE, "missing-backend")
    assert main([command, "spec"]) == 0
    output = capsys.readouterr()
    assert output.out == getdoc(getattr(specs, f"{command}_spec")) + "\n"
    assert output.err == ""
    assert recorded.requests == []

    with pytest.raises(SystemExit) as exited:
        main([command, "spec", "--help"])
    assert exited.value.code == 0
    assert output.out.rstrip() in capsys.readouterr().out


def test_generated_spec_documentation_matches_command_documentation(generator) -> None:
    assert (ROOT / "site/docs/cli-specs.md").read_text() == generator["render"]()
    assert (ROOT / "cli/client/src/engine/cli/specs.py").read_text() == generator["render_cli"]()
    assert (ROOT / "site/docs/cli-reference.md").read_text() == generator["render_reference"]()


def test_generated_markdown_cannot_break_mdx(generator) -> None:
    mdx, pipes = generator["mdx"], generator["pipes"]
    assert mdx(pipes("a ${x} or <b> | `${y} <c> | d`")) == r"a $\{x} or \<b> \| `${y} <c> \| d`"
    assert mdx("<a>\n```yaml\nx: ${y}\n```\n{b}") == "\\<a>\n```yaml\nx: ${y}\n```\n\\{b}"


def test_mdx_escaping_reaches_the_whole_site_page_but_not_cli_output(monkeypatch, generator) -> None:
    from dataclasses import replace
    from engine.graph_service import schema

    name, field = next(iter(schema.FIELD_RULES["loop"].items()))
    monkeypatch.setitem(schema.FIELD_RULES["loop"], name, replace(field, description="Uses {x} or <y>."))
    original = generator["specifications"].__globals__["run_path"]
    expressions = {"specification": lambda: "Write ${example} or <name>."}
    monkeypatch.setitem(generator["specifications"].__globals__, "run_path", lambda path: {
        "schema.py": vars(schema), "expressions.py": expressions}.get(Path(path).name) or original(path))
    site = generator["render"]()
    assert r"Uses \{x} or \<y>." in site and r"Write $\{example} or \<name>." in site
    cli = generator["specifications"]()
    assert "Uses {x} or <y>." in cli["graph"] and "Uses {x} or <y>." in cli["loop"]


def test_documented_graph_and_loop_examples_match_current_language() -> None:
    from inspect import getdoc
    import re
    import yaml
    from engine.cli.specs import graph_spec, loop_spec
    from engine.graph_service.language import API_VERSION, parse_graph

    graph = yaml.safe_load(re.search(r"```yaml\n(.*?)\n```", getdoc(graph_spec), re.S)[1])
    loop = yaml.safe_load(re.search(r"```yaml\n(.*?)\n```", getdoc(loop_spec), re.S)[1])
    assert graph["apiVersion"] == API_VERSION
    graph["loop"] = loop["loop"]
    parsed = parse_graph(graph, runners=["claude", "codex"])
    assert parsed.loop.interval_seconds == 6 * 60 * 60
    assert parsed.loop.instruction == loop["loop"]["instruction"]


def test_spec_generation_tracks_parser_definitions(tmp_path, monkeypatch, generator) -> None:
    source = ROOT / "cli/service/src/engine/graph_service"
    for name in ("schema.py", "expressions.py", "specification.py"):
        (tmp_path / name).write_text((source / name).read_text())
    with (tmp_path / "schema.py").open("a") as file:
        file.write('\nFIELDS["loop"].add("new_field")\nMIN_INTERVAL_SECONDS = 120\n')
    with (tmp_path / "expressions.py").open("a") as file:
        file.write('\nROOTS = ROOTS | {"new_root"}\n')

    parser = run_path(str(tmp_path / "expressions.py"))
    assert parser["parse"]("new_root").evaluate({"new_root": 42}) == 42
    monkeypatch.setitem(generator["specifications"].__globals__, "SERVICE", tmp_path)
    specs = generator["specifications"]()
    assert "`new_root`" in specs["graph"]
    assert "`new_field`" in specs["graph"]
    assert "`new_field`" in specs["loop"]
    assert "minimum of 120 seconds" in specs["loop"]


def test_field_constraints_drive_parser_and_reference(monkeypatch, generator) -> None:
    from dataclasses import replace
    from engine.graph_service import schema
    from engine.graph_service.language import GraphError, parse_graph

    graph = {
        "apiVersion": schema.API_VERSION, "name": "example",
        "implementation": {"work": {"agent": "claude", "prompt": "Do work",
                                   "outputs": {"answer": {}}}},
    }
    assert parse_graph(graph, runners=["claude"]).nodes[0].outputs[0].required is False
    monkeypatch.setitem(schema.FIELD_RULES["graph"], "name",
                        replace(schema.FIELD_RULES["graph"]["name"], pattern=r"^changed$"))
    monkeypatch.setitem(schema.FIELD_RULES["output"], "required",
                        replace(schema.FIELD_RULES["output"]["required"], default=True))
    with pytest.raises(GraphError):
        parse_graph(graph, runners=["claude"])
    graph["name"] = "changed"
    assert parse_graph(graph, runners=["claude"]).nodes[0].outputs[0].required is True

    original = generator["specifications"].__globals__["run_path"]

    def load(path):
        return vars(schema) if Path(path).name == "schema.py" else original(path)

    monkeypatch.setitem(generator["specifications"].__globals__, "run_path", load)
    text = generator["specifications"]()["graph"]
    assert "pattern: `^changed$`" in text
    assert "| output | required | Whether the value is required. | default: `True`; type: bool |" in text


def test_onboard_submits_and_polls_daemon(monkeypatch, capsys):
    backend = serve(monkeypatch,
        {"id": "job1", "status": "cloning", "message": "Cloning"},
        {"id": "job1", "status": "complete", "message": "Repository onboarded", "repository": "owner/repo", "path": "/code/repo"},
    )
    assert main(["repo", "add", "https://github.com/owner/repo", "--mode", "disconnected", "--path", "/code/repo"]) == 0
    assert backend.requests[0]["url"].endswith("/api/repositories/onboard")
    assert backend.requests[0]["body"] == {"repository": "https://github.com/owner/repo", "mode": "disconnected", "path": "/code/repo", "trusted": None}
    assert backend.requests[1]["url"].endswith("/api/repositories/onboard/job1")
    assert "Next: oe go review --project owner/repo --repo /code/repo" in capsys.readouterr().out


def test_onboard_no_url_reuses_current_github_checkout(monkeypatch):
    from engine.cli import repository
    monkeypatch.setattr(repository, "_git", lambda root, *args: "/checkout" if args[0] == "rev-parse" else "git@github.com:owner/repo.git")
    backend = serve(monkeypatch, {"status": "complete", "message": "Already onboarded", "repository": "owner/repo", "path": "/checkout"})
    assert main(["repo", "add", "--mode", "connected"]) == 0
    assert backend.requests[0]["body"]["path"] == "/checkout"
    assert backend.requests[0]["body"]["repository"] == "/checkout"


def test_onboard_reports_fix(monkeypatch, capsys):
    serve(monkeypatch, {"status": "failed", "message": "Preflight failed", "problems": [{"message": "No agent", "fix": "oe agent signin codex"}]})
    assert main(["repo", "add", "owner/repo", "--mode", "connected"]) == 1
    assert "oe agent signin codex" in capsys.readouterr().err


@pytest.mark.parametrize("stdin_tty,stdout_tty,flags,watched", [
    (True, True, [], True), (False, True, [], False), (True, False, [], False),
    (True, True, ["--wait"], False), (True, True, ["--json"], False),
    (True, True, ["--no-watch"], False),
])
def test_graph_run_live_selection(monkeypatch, capsys, stdin_tty, stdout_tty, flags, watched):
    import sys
    from engine.cli import live

    monkeypatch.setattr(sys.stdin, "isatty", lambda: stdin_tty)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: stdout_tty)
    done = {**RUN, "status": "completed", "terminal": True}
    serve(monkeypatch, RUN, done)
    calls = []
    monkeypatch.setattr(live.Watch, "follow", lambda self: calls.append(self.run_id) or (done, "finished"))
    assert main(["graph", "run", "pair", "go", "--repo", "o/r", *flags]) == 0
    assert calls == (["run-1"] if watched else [])
    output = capsys.readouterr().out
    if watched:
        assert "run-1 · completed" in output
    elif "--json" in flags:
        assert json.loads(output)["runId"] == "run-1"
    else:
        assert "run-1  pair v1" in output


@pytest.mark.parametrize("status,code", [("completed", 0), ("failed", 1), ("cancelled", 1), ("interrupted", 0), ("stopped", 0)])
def test_watch_finished_run(monkeypatch, capsys, status, code):
    serve(monkeypatch, {**RUN, "status": status, "terminal": True})
    assert main(["run", "watch", "run-1"]) == code
    assert f"run-1 · {status}" in capsys.readouterr().out


@pytest.mark.parametrize("outcome,code", [("detached", 0), ("cancelled", 1)])
def test_watch_outcome(monkeypatch, capsys, outcome, code):
    from engine.cli import live

    backends.add("mini", "http://mini:4364", use=True)
    monkeypatch.setattr(live.Watch, "follow", lambda self: (RUN, outcome))
    assert main(["run", "watch", "run-1"]) == code
    if outcome == "detached":
        assert "oe run watch run-1 --backend mini" in capsys.readouterr().out


def test_pretty_run_lists_approvals_and_ui(monkeypatch, capsys):
    serve(monkeypatch, {**RUN, "pendingApprovals": [
        {"approvalId": "a1", "node": "work", "command": "pytest"},
        {"approvalId": "a2", "node": "review", "command": "ruff check"},
    ]})
    assert main(["run", "get", "run-1", "--pretty"]) == 0
    out = capsys.readouterr().out
    assert "UI: http://127.0.0.1:4364/runs/run-1" in out
    assert "approval a1 at work: pytest" in out
    assert "approval a2 at review: ruff check" in out


@pytest.mark.parametrize("status,code", [
    ("completed", 0), ("failed", 1), ("cancelled", 1), ("interrupted", 0), ("stopped", 0),
])
@pytest.mark.parametrize("args", [["run", "get", "run-1"], ["run", "wait", "run-1"],
                                  ["graph", "run", "pair", "--wait"]])
def test_run_result_codes_match_watch(monkeypatch, status, code, args):
    run = {**RUN, "status": status, "terminal": True}
    serve(monkeypatch, run, run)
    assert main(args) == code


def test_pretty_approval_escapes_terminal_controls(monkeypatch, capsys):
    serve(monkeypatch, {**RUN, "pendingApprovals": [
        {"approvalId": "a\x1b[2K", "node": "work\r", "command": "danger\nbenign\x9b2K"},
    ]})
    assert main(["run", "get", "run-1", "--pretty"]) == 0
    out = capsys.readouterr().out
    assert r"approval a\x1b[2K at work\r: danger\nbenign\x9b2K" in out
    assert "\x1b" not in out and "\r" not in out and "\x9b" not in out


def test_local_run_sends_override_and_pretty_output_starts_with_mode(monkeypatch, capsys):
    context = {"mode": "disconnected", "reason": "explicitly requested local execution",
               "summary": "disconnected · local branch only (run oe connect github to publish)"}
    recorded = serve(monkeypatch, {**RUN, "forgeContext": context})
    assert main(["graph", "run", "pair", "do it", "--repo", "o/r", "--local", "--pretty"]) == 0
    assert recorded.requests[0]["body"]["inputs"] == {"mode": "disconnected"}
    assert capsys.readouterr().out.splitlines()[0] == context["summary"]


@pytest.mark.parametrize("backend_name", ["local", "mini"])
@pytest.mark.parametrize("explicit_repo", [False, True])
@pytest.mark.parametrize("local", [False, True])
def test_go_repository_backend_and_mode(monkeypatch, capsys, tmp_path, backend_name, explicit_repo, local):
    from engine.cli import repository
    backends.add("mini", "http://mini:4364")
    monkeypatch.setattr(repository, "current", lambda: repository.Repository(tmp_path, "owner/repo", "feature", False))
    mode = "disconnected" if local else "connected"
    run = {**RUN, "forgeContext": {"mode": mode, "summary": f"{mode} · owner/repo"}}
    expected_repo = "other/repo" if explicit_repo else str(tmp_path) if backend_name == "local" else "owner/repo"
    recorded = serve_go(monkeypatch, run, repo=expected_repo)
    args = ["go", "add docs", "--backend", backend_name, "-i", "spec=true"]
    if explicit_repo:
        args += ["--repo", "other/repo"]
    if local:
        args += ["--local"]
    assert main(args) == 0
    request = recorded.requests[-1]
    expected_repo = "other/repo" if explicit_repo else str(tmp_path) if backend_name == "local" else "owner/repo"
    assert request["body"]["repository"] == expected_repo
    assert request["body"]["graph"] == starters.DEFAULT
    assert request["body"]["inputs"] == {"spec": "true", **({"mode": "disconnected"} if local else {})}
    assert ("mini:4364" in request["url"]) == (backend_name == "mini")
    assert capsys.readouterr().out.splitlines()[0] == f"{starters.DEFAULT} · {mode} · {expected_repo} · " + ("will keep results and commits local" if local else "will open a pull request")


@pytest.mark.parametrize("alias,inputs", list(starters.ALIASES.items()))
def test_starter_aliases_use_default_graph(monkeypatch, capsys, alias, inputs):
    recorded = serve(monkeypatch, {"name": inputs[0], "source": starters.source(inputs[0])}, RUN)
    assert main(["graph", "run", alias, "task", "--repo", "o/r"]) == 0
    assert recorded.requests[-1]["body"]["graph"] == inputs[0]
    assert recorded.requests[-1]["body"]["inputs"] == inputs[1]
    assert "deprecated" in capsys.readouterr().err


@pytest.mark.parametrize("status,message", [(404, "no graph"), (503, "graph version gv-old is not loaded on this backend")])
def test_go_reregisters_missing_or_unloaded_default(monkeypatch, capsys, status, message):
    refused = HTTPError("http://x", status, message, {}, io.BytesIO(json.dumps({"error": message}).encode()))
    recorded = serve_go(monkeypatch, refused, {"created": True}, RUN)
    assert main(["go", "task", "--repo", "o/r"]) == 0
    first, registration, retry = recorded.requests[-3:]
    assert first["body"] == retry["body"]
    assert registration["body"]["format"] == "python"
    assert registration["body"]["source"] == starters.source()


def test_go_does_not_reregister_for_unrelated_service_failure(monkeypatch):
    refused = HTTPError("http://x", 503, "offline", {}, io.BytesIO(b'{"error":"backend offline"}'))
    recorded = serve_go(monkeypatch, refused)
    assert main(["go", "task", "--repo", "o/r"]) == 1
    assert len(recorded.requests) == 4


@pytest.mark.parametrize("option", ["review", "--review"])
@pytest.mark.parametrize("target,key", [("topic", "ref"), ("#9", "pr_url"), ("https://github.com/o/r/pull/9", "pr_url")])
def test_go_review_target(monkeypatch, target, key, option):
    resolved = "https://github.com/o/r/pull/9" if target == "#9" else target
    recorded = serve_go(monkeypatch, RUN, graph="review", target={key: resolved})
    assert main(["go", option, target, "--repo", "o/r"]) == 0
    assert recorded.requests[-1]["body"]["inputs"] == {key: resolved}
    assert recorded.requests[-1]["body"]["graph"] == "review"


@pytest.mark.parametrize('target', ['', 'topic'])
def test_go_review_disconnected_shows_local_banner(monkeypatch, capsys, tmp_path, target):
    from engine.cli import repository
    monkeypatch.setattr(repository, 'current', lambda: repository.Repository(tmp_path, 'o/r', 'topic', False))
    context = {'mode': 'disconnected',
               'summary': 'disconnected · local branch only (run oe connect github to publish)'}
    recorded = serve_go(monkeypatch, {**RUN, 'forgeContext': context}, repo=str(tmp_path),
                        graph='review', target={'ref': 'topic', 'mode': 'disconnected'})
    assert main(['go', 'review', *([target] if target else [])]) == 0
    assert capsys.readouterr().out.splitlines()[0] == 'review · disconnected · topic · will keep results and commits local'
    assert recorded.requests[-1]['body']['inputs'] == {'ref': 'topic'}


def test_go_wait_json_keeps_banner_off_stdout(monkeypatch, capsys):
    run = {**RUN, "forgeContext": {"mode": "connected", "summary": "connected · o/r"}}
    done = {**run, "status": "completed", "terminal": True, "prUrl": "https://github.com/o/r/pull/9"}
    serve_go(monkeypatch, run, done)
    assert main(["go", "docs", "--repo", "o/r", "--wait", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["prUrl"] == done["prUrl"]
    assert output.err.splitlines()[0] == f"{starters.DEFAULT} · connected · o/r · will open a pull request"


def test_go_live_banner_and_final_pull_request(monkeypatch, capsys):
    import sys
    from engine.cli import live
    run = {**RUN, "forgeContext": {"mode": "connected", "summary": "connected · o/r"}}
    serve_go(monkeypatch, run)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(live.Watch, "follow", lambda self: ({**run, "status": "completed", "prUrl": "https://github.com/o/r/pull/9"}, "completed"))
    assert main(["go", "docs", "--repo", "o/r"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0] == f"{starters.DEFAULT} · connected · o/r · will open a pull request"
    assert output.splitlines()[-1] == "Pull request: https://github.com/o/r/pull/9"


def test_go_wait_disconnected_reports_branch_and_push(monkeypatch, capsys):
    run = {**RUN, "forgeContext": {"mode": "disconnected", "summary": "disconnected · o/r"}}
    done = {**run, "status": "completed", "terminal": True, "branch": "agent/docs", "workspace": {"path": "/repo/worktree", "ref": "engine/ws-1"}}
    serve_go(monkeypatch, run, done)
    assert main(["go", "docs", "--repo", "o/r", "--local", "--wait"]) == 0
    output = capsys.readouterr().out
    assert output.splitlines()[0] == f"{starters.DEFAULT} · disconnected · o/r · will keep results and commits local"
    assert "Local branch: agent/docs" in output
    assert output.splitlines()[-1] == "To publish: git -C /repo/worktree push -u origin agent/docs"


def serve_go(monkeypatch, *answers, repo="o/r", graph=starters.DEFAULT, target=None):
    return serve(monkeypatch, {"verbs": starters.VERBS}, {"repo": repo, **(target or {})},
        {"name": graph, "source": starters.source(graph), "inputs": ["instruction", "ref", "pr_url", "spec", "adversarial", "review_threads"]}, *answers)


@pytest.mark.parametrize("words,verb,target,instruction", [
    (["review", "#839", "focus", "on migration"], "review", {"pr_url": "https://github.com/o/r/pull/839"}, "focus on migration"),
    (["review"], "review", {"ref": "topic"}, "Review this change"),
    (["review the auth module for races"], "go", {}, "review the auth module for races"),
    (["fix", "https://github.com/o/r/pull/9", "focus on tests"], "fix", {"pr_url": "https://github.com/o/r/pull/9", "ref": "origin/topic"}, "focus on tests"),
    (["fix", "#9"], "fix", {"pr_url": "https://github.com/o/r/pull/9", "ref": "origin/topic"}, "Fix this change"),
    (["fix", "topic"], "fix", {"ref": "topic"}, "Fix this change"),
])
def test_go_verb_grammar(monkeypatch, words, verb, target, instruction):
    recorded = serve_go(monkeypatch, RUN, graph=starters.VERBS[verb], target=target)
    assert main(["go", *words, "--repo", "o/r"]) == 0
    request = recorded.requests[-1]["body"]
    assert request["graph"] == starters.VERBS[verb]
    assert request["instruction"] == instruction
    assert request["inputs"] == target


def test_go_ambiguous_prose_and_missing_target(monkeypatch, capsys):
    message = "no such branch or PR; to give an instruction, quote it"
    def missing():
        return HTTPError("http://x", 404, message, {}, io.BytesIO(json.dumps({"error": message}).encode()))
    recorded = serve(monkeypatch, {"verbs": starters.VERBS}, missing(), {"repo": "o/r"},
        {"name": "implement", "source": starters.source(), "inputs": ["instruction"]}, RUN)
    assert main(["go", "review", "the", "auth", "module", "for", "races", "--repo", "o/r"]) == 0
    assert recorded.requests[-1]["body"]["instruction"] == "review the auth module for races"
    serve(monkeypatch, {"verbs": starters.VERBS}, missing())
    assert main(["go", "review", "foo", "--repo", "o/r"]) == 1
    assert message in capsys.readouterr().err


def test_verb_binding_checks_contract_before_writing(monkeypatch, capsys):
    recorded = serve(monkeypatch, {"name": "custom", "inputs": ["instruction"]})
    assert main(["verb", "set", "review", "custom"]) == 2
    assert "must declare pr_url or ref" in capsys.readouterr().err
    assert len(recorded.requests) == 1
    recorded = serve(monkeypatch, {"name": "custom", "inputs": ["ref"]}, {"verbs": {"review": "custom"}})
    assert main(["verb", "set", "review", "custom"]) == 0
    assert recorded.requests[-1]["body"]["graph"] == "custom"


@pytest.mark.parametrize('origin', ['', 'https://bitbucket.org/team/repo.git', 'https://gitlab.com/group/repo.git'])
@pytest.mark.parametrize('relative', [True, False])
def test_repo_add_existing_path(tmp_path, monkeypatch, relative, origin):
    from engine.cli import repository
    monkeypatch.chdir(tmp_path)
    calls = []
    def git(path, *args):
        calls.append((path, args))
        return str(tmp_path) if args[0] == 'rev-parse' else origin
    monkeypatch.setattr(repository, '_git', git)
    backend = serve(monkeypatch, {'status': 'complete', 'message': 'Repository onboarded', 'repository': 'gitlab.com/group/repo', 'path': str(tmp_path)})
    assert main(['repo', 'add', '.' if relative else str(tmp_path)]) == 0
    assert calls[0][0] == str(tmp_path)
    assert backend.requests[0]['body'] == {'repository': str(tmp_path), 'path': str(tmp_path), 'mode': None, 'trusted': None}


def test_repo_add_gitlab_url(monkeypatch):
    backend = serve(monkeypatch, {'status': 'complete', 'message': 'Repository onboarded', 'repository': 'gitlab.com/group/repo', 'path': '/code/repo'})
    assert main(['repo', 'add', 'https://gitlab.com/group/repo.git']) == 0
    assert backend.requests[0]['body']['repository'] == 'https://gitlab.com/group/repo.git'
    assert backend.requests[0]['body']['path'] is None


@pytest.mark.parametrize("outcome", ["finished", "cancelled"])
def test_workspace_hint_preserves_explicit_backend(monkeypatch, capsys, outcome):
    import shlex
    from engine.cli import live

    backends.add("remote-machine", "http://remote:4364")
    run = {**RUN, "workspace": {"path": "/tmp/workspace"}}
    monkeypatch.setattr(live.Watch, "follow", lambda self: (run, outcome))
    main(["run", "watch", "run-1", "--backend", "remote-machine"])
    hint = next(line for line in capsys.readouterr().out.splitlines()
                if line.startswith("Workspace details: "))
    assert shlex.split(hint.removeprefix("Workspace details: ")) == [
        "oe", "run", "get", "run-1", "--backend", "remote-machine",
    ]


def test_onboarding_401_explains_separate_credential(monkeypatch, capsys):
    serve(monkeypatch, HTTPError('http://localhost/api/repositories/onboard', 401, '', {}, io.BytesIO(b'{}')))
    assert main(['repo', 'add', 'owner/repo']) == 1
    assert 'ENGINE_ONBOARDING_TOKEN' in capsys.readouterr().err


def test_onboard_uses_separate_backend_token_for_submit_and_poll(monkeypatch):
    monkeypatch.setenv('ENGINE_SERVICE_TOKEN', 's' * 40)
    monkeypatch.setenv('ENGINE_ONBOARDING_TOKEN', 'o' * 40)
    assert main(['backend', 'add', 'onboarding', 'http://localhost:4364', '--token-env', 'ENGINE_ONBOARDING_TOKEN']) == 0
    backend = serve(monkeypatch,
        {'id': 'job1', 'status': 'cloning', 'message': 'Cloning'},
        {'id': 'job1', 'status': 'complete', 'message': 'Repository onboarded', 'repository': 'owner/repo', 'path': '/repo'},
    )
    assert main(['repo', 'add', 'owner/repo', '--backend', 'onboarding']) == 0
    assert [request['authorization'] for request in backend.requests] == ['Bearer ' + 'o' * 40] * 2


def test_onboarding_next_command_uses_review_backend(monkeypatch, capsys):
    import shlex

    monkeypatch.setenv('ENGINE_ONBOARDING_TOKEN', 'o' * 40)
    monkeypatch.setenv('WORK_SERVICE_TOKEN', 's' * 40)
    backends.add('onboarding', 'http://work:4364', token_env='ENGINE_ONBOARDING_TOKEN')
    backends.add('work', 'http://work:4364', token_env='WORK_SERVICE_TOKEN')
    recorded = serve(monkeypatch,
        {'status': 'complete', 'message': 'Repository onboarded', 'repository': 'owner/repo', 'path': '/code/repo'},
        RUN,
    )
    assert main(['repo', 'add', 'owner/repo', '--backend', 'onboarding', '--review-backend', 'work']) == 0
    command = capsys.readouterr().out.split('Next: ', 1)[1].strip()
    assert recorded.requests[0]['authorization'] == 'Bearer ' + 'o' * 40
    review = serve_go(monkeypatch, RUN, graph=starters.VERBS['review'], target={'ref': 'HEAD'})
    assert main(shlex.split(command)[1:]) == 0
    assert all(request['authorization'] == 'Bearer ' + 's' * 40 for request in review.requests)
    assert all(request['url'].startswith('http://work:4364/api/v1/') for request in review.requests)


def test_onboarding_without_review_backend_does_not_suggest_invalid_command(monkeypatch, capsys):
    backends.add('onboarding', 'http://work:4364', token_env='ENGINE_ONBOARDING_TOKEN')
    serve(monkeypatch, {'status': 'complete', 'message': 'Repository onboarded', 'repository': 'owner/repo', 'path': '/code/repo'})
    assert main(['repo', 'add', 'owner/repo', '--backend', 'onboarding']) == 0
    output = capsys.readouterr().out
    assert 'Next: oe go' not in output
    assert '--review-backend' in output


def test_review_backend_rejects_onboarding_credentials_before_submit(monkeypatch, capsys):
    backends.add('onboarding', 'http://work:4364', token_env='ENGINE_ONBOARDING_TOKEN')
    recorded = serve(monkeypatch)
    assert main(['repo', 'add', 'owner/repo', '--review-backend', 'onboarding']) == 2
    assert 'must use service credentials' in capsys.readouterr().err
    assert recorded.requests == []

"""The read-only contract for the first `engine` terminal-client slice."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

from engine.apps.cli import __main__ as cli


class _Response:
    def __init__(self, payload: dict[object, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class _StreamResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter([b'{"type": "done", "content": []}\n'])


class _ContentThenDoneResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter([
            b'{"type": "content", "content": [{"text": "Working answer"}]}\n',
            b'{"type": "done", "content": [{"text": "Working answer"}]}\n',
        ])


def test_status_json_identifies_a_ready_compatible_service(monkeypatch, capsys):
    monkeypatch.setattr(cli, "urlopen", lambda *_args, **_kwargs: _Response({
        "service": "openengine", "version": "1.2.3", "ready": True, "api_version": 1,
    }))

    assert cli.main(["status", "--json", "--server", "http://engine.test"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "checks": [{"name": "service", "ok": True, "detail": "OpenEngine 1.2.3 is ready"}],
        "identity": {"service": "openengine", "version": "1.2.3", "ready": True, "api_version": 1},
        "server": "http://engine.test",
        "started": False,
    }


def test_status_rejects_an_occupied_port_that_is_not_openengine(monkeypatch, capsys):
    monkeypatch.setattr(cli, "urlopen", lambda *_args, **_kwargs: _Response({"service": "other"}))

    assert cli.main(["status", "--server", "http://127.0.0.1:4364"]) == 1

    assert "not an OpenEngine service" in capsys.readouterr().out


def test_status_reports_a_remote_connection_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("unreachable")))

    assert cli.main(["status", "--json", "--server", "https://example.invalid"]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["server"] == "https://example.invalid"
    assert report["checks"][0]["ok"] is False


def test_connections_reports_github_and_slack_readiness(monkeypatch, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: ("http://engine.test", ready))
    responses = {
        "/api/github/status": {"connected": True, "clientIdConfigured": True},
        "/api/source-control/status": {
            "provider": "gh-cli",
            "ghCli": {"authenticated": True, "account": "vadym"},
        },
        "/api/gitlab/status": {
            "origin": "https://gitlab.com", "connected": False, "clientIdConfigured": False,
        },
        "/api/slack/status": {"configured": True, "connected": True, "events": True},
    }
    monkeypatch.setattr(cli, "fetch_json", lambda _server, path: responses[path])

    assert cli.main(["connections", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {"connections": {
        "github": responses["/api/github/status"],
        "sourceControl": responses["/api/source-control/status"],
        "gitlab": responses["/api/gitlab/status"],
        "slack": responses["/api/slack/status"],
    }}


def test_connections_human_output_explains_slack_event_readiness(capsys):
    cli.render_connections({
        "github": {"connected": False, "clientIdConfigured": True},
        "sourceControl": {"provider": "github-oauth", "ghCli": {}},
        "gitlab": {"origin": "https://gitlab.com", "connected": False, "clientIdConfigured": False},
        "slack": {"configured": True, "connected": True, "events": False},
    }, False)

    output = capsys.readouterr().out
    assert "GitHub OAuth: not connected" in output
    assert "Slack: connected; events not ready" in output


def test_transcript_shows_user_and_agent_messages_but_not_tool_calls(monkeypatch, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: ("http://engine.test", ready))
    monkeypatch.setattr(cli, "fetch_json", lambda *_args: {"messages": [
        {"id": "user-1", "role": "user", "content": [{"type": "text", "text": "Hello"}]},
        {"id": "agent-1", "role": "assistant", "content": [
            {"type": "tool-call", "toolName": "read_file"},
            {"type": "text", "text": "I read the file."},
        ]},
    ]})

    assert cli.main(["transcript", "thread-1"]) == 0

    output = capsys.readouterr().out
    assert "You\n  Hello" in output
    assert "OpenEngine\n  I read the file." in output
    assert "read_file" not in output


def test_transcript_json_omits_tool_calls(monkeypatch, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: ("http://engine.test", ready))
    monkeypatch.setattr(cli, "fetch_json", lambda *_args: {"messages": [
        {"id": "agent-1", "role": "assistant", "content": [
            {"type": "tool-call", "toolName": "read_file"},
            {"type": "text", "text": "Done"},
        ]},
    ]})

    assert cli.main(["transcript", "thread-1", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {"messages": [{
        "id": "agent-1", "role": "assistant", "content": [{"type": "text", "text": "Done"}],
    }]}


def test_config_server_persists_selected_profile(monkeypatch, tmp_path: Path, capsys):
    path = tmp_path / "cli.json"
    monkeypatch.setenv(cli.CONFIG_ENVIRONMENT_VARIABLE, str(path))

    assert cli.main(["config", "server", "https://engine.example/"]) == 0
    assert cli.load_preferences(path).profile().server == "https://engine.example"
    assert "default" in capsys.readouterr().out


def test_config_profile_switches_the_server_preference(monkeypatch, tmp_path: Path):
    path = tmp_path / "cli.json"
    monkeypatch.setenv(cli.CONFIG_ENVIRONMENT_VARIABLE, str(path))

    assert cli.main(["config", "profile", "staging"]) == 0
    assert cli.main(["config", "server", "https://staging.example"]) == 0

    preferences = cli.load_preferences(path)
    assert preferences.selected_profile == "staging"
    assert preferences.profile().server == "https://staging.example"


def test_default_local_status_starts_one_service_when_nothing_responds(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(cli.STATE_ENVIRONMENT_VARIABLE, str(tmp_path))
    unavailable = cli.Check("service", False, "cannot reach local service")
    ready = cli.Check("service", True, "OpenEngine 1.2.3 is ready")
    identity = {"service": "openengine", "version": "1.2.3", "ready": True, "api_version": 1}
    responses = iter([(unavailable, None), (unavailable, None)])
    monkeypatch.setattr(cli, "probe", lambda *_args, **_kwargs: next(responses))
    started = []
    monkeypatch.setattr(cli, "launch_local_service", lambda server: started.append(server) or object())
    monkeypatch.setattr(cli, "wait_until_ready", lambda *_args: (ready, identity))

    check, actual_identity, launched = cli.ensure_service(cli.DEFAULT_SERVER)

    assert check.ok is True
    assert actual_identity == identity
    assert launched is True
    assert started == [cli.DEFAULT_SERVER]


def test_explicit_remote_service_never_starts_a_local_process(monkeypatch):
    unavailable = cli.Check("service", False, "cannot reach remote service")
    monkeypatch.setattr(cli, "probe", lambda *_args, **_kwargs: (unavailable, None))
    monkeypatch.setattr(cli, "launch_local_service", lambda *_args: (_ for _ in ()).throw(AssertionError("must not start")))

    check, identity, launched = cli.ensure_service("https://example.invalid")

    assert check is unavailable
    assert identity is None
    assert launched is False


def test_an_occupied_default_port_with_another_http_service_is_not_replaced(monkeypatch):
    occupied = cli.Check("service", False, "endpoint is not an OpenEngine service")
    monkeypatch.setattr(cli, "probe", lambda *_args, **_kwargs: (occupied, {"service": "other"}))
    monkeypatch.setattr(cli, "launch_local_service", lambda *_args: (_ for _ in ()).throw(AssertionError("must not start")))

    check, identity, launched = cli.ensure_service(cli.DEFAULT_SERVER)

    assert check is occupied
    assert identity == {"service": "other"}
    assert launched is False


def test_stale_startup_lock_is_recovered(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(cli.STATE_ENVIRONMENT_VARIABLE, str(tmp_path))
    lock = tmp_path / "startup.lock"
    lock.write_text("999999")
    monkeypatch.setattr(cli, "process_alive", lambda _pid: False)

    with cli.startup_lock():
        assert lock.exists()

    assert not lock.exists()


def test_threads_lists_active_threads_as_json(monkeypatch, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "ensure_service", lambda _server: (ready, {}, False))
    monkeypatch.setattr(cli, "urlopen", lambda *_args, **_kwargs: _Response({"threads": [
        {"id": "active", "title": "Active task", "archived": False},
        {"id": "archived", "title": "Archived task", "archived": True},
    ]}))

    assert cli.main(["threads", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == {"threads": [
        {"id": "active", "title": "Active task", "archived": False},
    ]}


def test_task_remembers_the_opened_thread(monkeypatch, tmp_path: Path, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setenv(cli.CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "cli.json"))
    monkeypatch.setattr(cli, "ensure_service", lambda _server: (ready, {}, False))
    monkeypatch.setattr(cli, "urlopen", lambda *_args, **_kwargs: _Response({
        "id": "thread-1", "title": "Inspect me", "workspaceRoot": "/work/repo",
        "runner": "codex", "archived": False, "phase": "running",
        "currentRun": {"id": "run-1", "phase": "running"}, "previousRuns": [],
        "pendingApproval": True,
    }))

    assert cli.main(["task", "thread-1", "--json"]) == 0

    assert cli.load_preferences().profile().last_task == "thread-1"
    assert json.loads(capsys.readouterr().out)["title"] == "Inspect me"


def test_palette_filters_then_selects_with_arrow_keys(monkeypatch):
    keys = iter(["t", "up", "enter"])
    monkeypatch.setattr(cli, "read_key", lambda: next(keys))

    assert cli.palette(["/help", "/status", "/threads"], "Command: ") == "/threads"


def test_palette_shows_all_slash_commands_as_soon_as_slash_is_typed(monkeypatch, capsys):
    keys = iter(["s", "t", "a", "t", "u", "s", "enter"])
    monkeypatch.setattr(cli, "read_key", lambda: next(keys))

    assert cli.palette(
        ["/help", "/status", "/threads"], "Command: ", initial_query="/"
    ) == "/status"

    first_frame = capsys.readouterr().out.split("\x1b[2J\x1b[H", 2)[1]
    assert "/help" in first_frame
    assert "/status" in first_frame
    assert "/threads" in first_frame


def test_interactive_opens_the_command_palette_on_slash_without_enter(monkeypatch):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "read_key", lambda: "/")
    seen = []
    monkeypatch.setattr(
        cli,
        "palette",
        lambda _options, _prompt, *, initial_query="": seen.append(initial_query) or "/quit",
    )

    assert cli.interactive(cli.argparse.Namespace(server=None), cli.Preferences()) == 0
    assert seen == ["/"]


def test_interactive_ctrl_z_quits_without_opening_the_palette(monkeypatch):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "read_key", lambda: "quit")
    monkeypatch.setattr(cli, "palette", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not open")))

    assert cli.interactive(cli.argparse.Namespace(server=None), cli.Preferences()) == 0


def test_palette_ctrl_z_selects_quit(monkeypatch):
    monkeypatch.setattr(cli, "read_key", lambda: "quit")

    assert cli.palette(["/status", "/quit"], "Command: ") == "/quit"


def test_interactive_escape_returns_from_the_command_palette_to_the_prompt(monkeypatch, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    keys = iter(["/", "escape", "quit"])
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "read_key", lambda: next(keys))

    assert cli.interactive(cli.argparse.Namespace(server=None), cli.Preferences()) == 0
    assert "service readiness" not in capsys.readouterr().out


def test_interactive_escape_from_setup_reopens_the_command_palette(monkeypatch):
    ready = cli.Check("service", True, "OpenEngine is ready")
    commands = iter(["/setup", "/quit"])
    prompts = []
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "read_key", lambda: "/")

    def choose(_options, prompt, *, initial_query=""):
        prompts.append((prompt, initial_query))
        return None if prompt == "Provider: " else next(commands)

    monkeypatch.setattr(cli, "palette", choose)

    assert cli.interactive(cli.argparse.Namespace(server=None), cli.Preferences()) == 0
    assert prompts == [("Command: ", "/"), ("Provider: ", ""), ("Command: ", "/")]


def test_run_creates_a_thread_and_streams_the_prompt(monkeypatch, tmp_path: Path, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setenv(cli.CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "cli.json"))
    monkeypatch.setattr(cli, "read_service", lambda *_args: ("http://engine.test", ready))
    monkeypatch.setattr(cli, "creation_defaults", lambda *_args: ("coder", "codex", "/repo"))
    monkeypatch.setattr(cli, "request_json", lambda *_args: {"id": "thread-1", "title": "New chat"})
    seen = []
    monkeypatch.setattr(cli, "stream_run", lambda server, path, body=None: seen.append((server, path, body)) or 0)

    assert cli.main(["run", "Ship it"]) == 0

    assert seen == [("http://engine.test", "/api/threads/thread-1/runs", {"text": "Ship it", "runner": "codex"})]
    assert cli.load_preferences().profile().last_task == "thread-1"
    assert "Started New chat" in capsys.readouterr().out


def test_stream_run_shows_a_spinner_until_the_first_event(monkeypatch):
    monkeypatch.setattr(cli, "urlopen", lambda *_args, **_kwargs: _StreamResponse())
    events = []

    class Spinner:
        stopped = False

        def start(self):
            events.append("start")

        def stop(self):
            if not self.stopped:
                self.stopped = True
                events.append("stop")

    monkeypatch.setattr(cli, "TerminalSpinner", Spinner)

    assert cli.stream_run("http://engine.test", "/api/threads/thread-1/runs", {"text": "Ship it"}) == 0
    assert events == ["start", "stop"]


def test_stream_run_does_not_repeat_the_final_content_snapshot(monkeypatch, capsys):
    monkeypatch.setattr(cli, "urlopen", lambda *_args, **_kwargs: _ContentThenDoneResponse())

    assert cli.stream_run("http://engine.test", "/api/threads/thread-1/runs") == 0

    assert capsys.readouterr().out.count("Working answer") == 1


def test_run_defaults_a_local_task_repository_to_the_current_directory(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "fetch_json", lambda *_args: {
        "defaultAgent": "coder",
        "defaultRunner": "codex",
        "repositories": [{"path": "/configured/repository"}],
    })

    agent, runner, repository = cli.creation_defaults(
        cli.DEFAULT_SERVER,
        cli.Preferences(profiles={"default": cli.Profile(last_repository="/remembered/repository")}),
        type("Arguments", (), {"agent": None, "runner": None, "repository": None})(),
    )

    assert (agent, runner, repository) == ("coder", "codex", str(tmp_path.resolve()))


def test_run_does_not_send_the_current_directory_to_a_remote_server(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "fetch_json", lambda *_args: {
        "defaultAgent": "coder",
        "defaultRunner": "codex",
        "repositories": [{"path": "/configured/repository"}],
    })

    _agent, _runner, repository = cli.creation_defaults(
        "https://engine.example",
        cli.Preferences(),
        type("Arguments", (), {"agent": None, "runner": None, "repository": None})(),
    )

    assert repository == "/configured/repository"


def test_resume_reconnects_without_issuing_a_cancellation(monkeypatch):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: ("http://engine.test", ready))
    monkeypatch.setattr(cli, "fetch_json", lambda *_args: {"id": "thread-1"})
    seen = []
    monkeypatch.setattr(cli, "stream_run", lambda server, path, body=None: seen.append((server, path, body)) or 0)

    assert cli.main(["resume", "thread-1"]) == 0

    assert seen == [("http://engine.test", "/api/threads/thread-1/runs/current", None)]


def test_doctor_reports_prerequisites_and_keeps_a_stable_exit_code(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv(cli.CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "config" / "cli.json"))
    monkeypatch.setattr(cli, "user_data_path", lambda _name: tmp_path / "data")
    def response(request, **_kwargs):
        url = request.full_url if hasattr(request, "full_url") else request
        if url.endswith("/api/source-control/status"):
            return _Response({"provider": "gh-cli"})
        return _Response({"service": "openengine", "version": "1.2.3", "ready": True, "api_version": 1})

    monkeypatch.setattr(cli, "urlopen", response)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/tools/{name}")

    assert cli.main(["doctor", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert {check["name"] for check in report["checks"]} == {
        "service", "git", "codex", "claude", "source_control", "config", "data",
    }

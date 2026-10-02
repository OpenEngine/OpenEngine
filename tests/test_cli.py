"""The read-only contract for the first `engine` terminal-client slice."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError

import pytest

from engine.apps.cli import __main__ as cli
from engine.apps.cli import daemon


@pytest.fixture(autouse=True)
def isolated_daemon_state(monkeypatch, tmp_path: Path) -> None:
    """Never let a real `engine daemon` record on this machine steer these tests."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))


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
    monkeypatch.setattr(daemon, "urlopen", lambda *_args, **_kwargs: _Response({
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
    monkeypatch.setattr(daemon, "urlopen", lambda *_args, **_kwargs: _Response({"service": "other"}))

    assert cli.main(["status", "--server", "http://127.0.0.1:4364"]) == 1

    assert "not an OpenEngine service" in capsys.readouterr().out


def test_status_reports_a_remote_connection_failure(monkeypatch, capsys):
    monkeypatch.setattr(daemon, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("unreachable")))

    assert cli.main(["status", "--json", "--server", "https://example.invalid"]) == 1

    report = json.loads(capsys.readouterr().out)
    assert report["server"] == "https://example.invalid"
    assert report["checks"][0]["ok"] is False


def test_connection_lines_explain_slack_event_readiness():
    output = "\n".join(cli.connection_lines({
        "github": {"connected": False, "clientIdConfigured": True},
        "sourceControl": {"provider": "github-oauth", "ghCli": {}},
        "gitlab": {"origin": "https://gitlab.com", "connected": False, "clientIdConfigured": False},
        "slack": {"configured": True, "connected": True, "events": False},
    }))

    assert "Active provider: GitHub OAuth (not connected)" in output
    assert "Slack: connected; events not ready" in output


def test_connection_lines_label_connected_but_unselected_provider_as_available():
    output = "\n".join(cli.connection_lines({
        "github": {"connected": True, "clientIdConfigured": True},
        "sourceControl": {"provider": "github-oauth", "ghCli": {}},
        "gitlab": {"origin": "https://gitlab.com", "connected": True, "clientIdConfigured": True},
        "slack": {"configured": False, "connected": False},
    }))

    assert "Active provider: GitHub OAuth (connected)" in output
    assert "GitLab (https://gitlab.com): connected — available, not active" in output


def test_connection_lines_show_individual_loading_indicators():
    lines = cli.connection_lines({}, loading={"github", "sourceControl", "gitlab", "slack"}, spinner="⠹")

    assert "⠹  Active provider: checking…" in lines
    assert "⠹  GitHub OAuth: checking…" in lines
    assert "⠹  GitLab (https://gitlab.com): checking…" in lines
    assert "⠹  Slack: checking…" in lines


def test_transcript_shows_user_and_agent_messages_but_not_tool_calls(capsys):
    cli.render_transcript([
        {"id": "user-1", "role": "user", "content": [{"type": "text", "text": "Hello"}]},
        {"id": "agent-1", "role": "assistant", "content": [
            {"type": "tool-call", "toolName": "read_file"},
            {"type": "text", "text": "I read the file."},
        ]},
    ], False)

    output = capsys.readouterr().out
    assert "› Hello" in output
    assert "• OpenEngine\n  I read the file." in output
    assert "read_file" not in output


def test_transcript_json_omits_tool_calls(capsys):
    cli.render_transcript([
        {"id": "agent-1", "role": "assistant", "content": [
            {"type": "tool-call", "toolName": "read_file"},
            {"type": "text", "text": "Done"},
        ]},
    ], True)

    assert json.loads(capsys.readouterr().out) == {"messages": [{
        "id": "agent-1", "role": "assistant", "content": [{"type": "text", "text": "Done"}],
    }]}


def test_legacy_engine_config_environment_variable_overrides_preferences_path(monkeypatch, tmp_path: Path):
    path = tmp_path / "isolated-cli.json"
    monkeypatch.setenv(cli.LEGACY_CONFIG_ENVIRONMENT_VARIABLE, str(path))

    assert cli.preferences_path() == path


def test_engine_cli_config_takes_precedence_over_legacy_engine_config(monkeypatch, tmp_path: Path):
    preferred = tmp_path / "preferred-cli.json"
    monkeypatch.setenv(cli.LEGACY_CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "legacy-cli.json"))
    monkeypatch.setenv(cli.CONFIG_ENVIRONMENT_VARIABLE, str(preferred))

    assert cli.preferences_path() == preferred


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


def test_startup_dashboard_prioritizes_work_orders_that_need_approval(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_threads", lambda *_args: [
        {"id": "waiting", "title": "Publish release", "pendingApproval": True},
        {"id": "running", "title": "Improve CLI", "phase": "running"},
    ])
    monkeypatch.setattr(cli, "thread_summary", lambda _server, thread: str(thread["title"]))

    cli.render_startup_dashboard("http://engine.test")

    output = capsys.readouterr().out
    assert "Action required" in output
    assert "Publish release — approval required" in output
    assert "Use /approvals to review pending decisions." in output
    assert "Improve CLI — running" in output


def test_thread_summary_uses_the_opening_user_message_for_a_new_chat(monkeypatch):
    monkeypatch.setattr(cli, "load_transcript", lambda *_args: [
        {"role": "user", "content": [{"type": "text", "text": "  Improve\n the CLI dashboard  "}]},
    ])

    assert cli.thread_summary("http://engine.test", {"id": "thread-1", "title": "New chat"}) == "Improve the CLI dashboard"


def test_thread_choice_labels_use_summaries_and_disambiguate_duplicates():
    threads = [{"id": "first", "title": "New chat"}, {"id": "second", "title": "New chat"}]

    labels = cli.thread_choice_labels(threads, {"first": "Improve CLI", "second": "Improve CLI"})

    assert list(labels) == ["Improve CLI", "Improve CLI (2)"]
    assert labels["Improve CLI (2)"]["id"] == "second"


def test_open_thread_shows_the_transcript_then_continues_the_same_work_order(monkeypatch):
    thread = {"id": "thread-1", "title": "Improve CLI"}
    monkeypatch.setattr(cli, "remember_thread", lambda *_args: None)
    monkeypatch.setattr(cli, "load_transcript", lambda *_args: [])
    monkeypatch.setattr(cli, "prompt_line", lambda _prompt: "Add the dashboard")
    streamed = []
    monkeypatch.setattr(
        cli, "stream_run", lambda server, path, body=None: streamed.append((server, path, body)) or 0
    )

    cli.open_thread("http://engine.test", thread, cli.Preferences())

    assert streamed == [
        ("http://engine.test", "/api/threads/thread-1/runs", {"text": "Add the dashboard"}),
    ]


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

    output = capsys.readouterr().out
    assert "\x1b[2J" not in output
    first_frame = output.split("\r\x1b[2K", 1)[1]
    assert "/help" in first_frame
    assert "/status" in first_frame
    assert "/threads" in first_frame


def test_palette_renders_command_descriptions_in_two_columns():
    lines = cli.palette_lines(["/status", "/quit"], "engine> ", "/", 0)

    assert "Check whether the OpenEngine service is ready" in lines[2]
    assert "Exit the CLI" in lines[3]


def test_interactive_opens_the_command_palette_on_slash_without_enter(monkeypatch):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "render_startup_dashboard", lambda _server: None)
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
    monkeypatch.setattr(cli, "render_startup_dashboard", lambda _server: None)
    monkeypatch.setattr(cli, "read_key", lambda: "quit")
    monkeypatch.setattr(cli, "palette", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not open")))

    assert cli.interactive(cli.argparse.Namespace(server=None), cli.Preferences()) == 0


def test_interactive_ignores_arrow_keys_outside_the_command_palette(monkeypatch, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    keys = iter(["up", "down", "quit"])
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "render_startup_dashboard", lambda _server: None)
    monkeypatch.setattr(cli, "read_key", lambda: next(keys))

    assert cli.interactive(cli.argparse.Namespace(server=None), cli.Preferences()) == 0

    assert "Use / to open" not in capsys.readouterr().out


def test_interactive_message_creates_a_new_work_order(monkeypatch):
    ready = cli.Check("service", True, "OpenEngine is ready")
    keys = iter(["H", "i", "enter", "quit"])
    created = []
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "render_startup_dashboard", lambda _server: None)
    monkeypatch.setattr(cli, "read_key", lambda: next(keys))
    monkeypatch.setattr(cli, "run", lambda arguments, _preferences: created.append(arguments.prompt) or 0)

    assert cli.interactive(cli.argparse.Namespace(server=None), cli.Preferences()) == 0

    assert created == ["Hi"]


def test_palette_ctrl_z_selects_quit(monkeypatch):
    monkeypatch.setattr(cli, "read_key", lambda: "quit")

    assert cli.palette(["/status", "/quit"], "Command: ") == "/quit"


def test_prompt_line_escape_cancels_without_inserting_an_escape_character(monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_key", lambda: "escape")

    assert cli.prompt_line("Thread ID: ") is None

    assert "^[" not in capsys.readouterr().out


def test_interactive_escape_returns_from_the_command_palette_to_the_prompt(monkeypatch, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    keys = iter(["/", "escape", "quit"])
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "render_startup_dashboard", lambda _server: None)
    monkeypatch.setattr(cli, "read_key", lambda: next(keys))

    assert cli.interactive(cli.argparse.Namespace(server=None), cli.Preferences()) == 0
    assert "service readiness" not in capsys.readouterr().out


def test_interactive_escape_from_settings_reopens_the_command_palette(monkeypatch):
    ready = cli.Check("service", True, "OpenEngine is ready")
    commands = iter(["/settings", "/quit"])
    prompts = []
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "render_startup_dashboard", lambda _server: None)
    monkeypatch.setattr(cli, "read_key", lambda: "/")
    monkeypatch.setattr(cli, "progressive_connection_snapshot", lambda _server: {})

    def choose(_options, prompt, *, initial_query=""):
        prompts.append((prompt, initial_query))
        return None if prompt == "Settings: " else next(commands)

    monkeypatch.setattr(cli, "palette", choose)

    assert cli.interactive(cli.argparse.Namespace(server=None), cli.Preferences()) == 0
    assert prompts == [("engine> ", "/"), ("Settings: ", ""), ("engine> ", "/")]


def test_connect_slack_opens_the_authorization_url_and_waits_for_connection(monkeypatch, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: ("http://engine.test", ready))
    monkeypatch.setattr(cli, "request_json", lambda *_args: {"authorizationUrl": "https://slack.example/oauth"})
    monkeypatch.setattr(cli, "fetch_json", lambda *_args: {"connected": True})
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", opened.append)

    assert cli.connect(
        cli.argparse.Namespace(server=None, provider="slack", origin="https://gitlab.com", open=True), cli.Preferences()
    ) == 0

    assert opened == ["https://slack.example/oauth"]
    assert "Connected." in capsys.readouterr().out


def test_connect_github_explains_the_keychain_and_waits_a_minute_for_it(monkeypatch, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: ("http://engine.test", ready))
    requests = []

    def request(_server, path, _body, timeout=10.0):
        requests.append((path, timeout))
        if path == "/api/github/connect":
            return {"verificationUri": "https://github.com/login/device", "userCode": "CODE", "interval": 0}
        return {"status": "complete"}

    monkeypatch.setattr(cli, "request_json", request)
    monkeypatch.setattr(cli, "post_empty", lambda *_args: None)

    assert cli.connect(
        cli.argparse.Namespace(server=None, provider="github", origin="https://gitlab.com", open=False), cli.Preferences()
    ) == 0

    assert requests == [("/api/github/connect", 60.0), ("/api/github/connect/poll", 60.0)]
    out = capsys.readouterr().out
    assert "system keychain" in out and "login password" in out


def test_run_creates_a_thread_and_streams_the_prompt(monkeypatch, tmp_path: Path, capsys):
    ready = cli.Check("service", True, "OpenEngine is ready")
    monkeypatch.setenv(cli.CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "cli.json"))
    monkeypatch.setattr(cli, "read_service", lambda *_args: ("http://engine.test", ready))
    monkeypatch.setattr(cli, "creation_defaults", lambda *_args: ("coder", "codex", "/repo"))
    monkeypatch.setattr(cli, "request_json", lambda *_args: {"id": "thread-1", "title": "New chat"})
    seen = []
    monkeypatch.setattr(cli, "stream_run", lambda server, path, body=None: seen.append((server, path, body)) or 0)

    assert cli.run(
        cli.argparse.Namespace(server=None, prompt="Ship it", agent=None, runner=None, repository=None), cli.Preferences()
    ) == 0

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


def test_terminal_spinner_cycles_until_stopped(monkeypatch):
    spinner = cli.TerminalSpinner()
    waits = []

    class StopEvent:
        def is_set(self):
            return False

        def wait(self, _seconds):
            waits.append(True)
            return len(waits) == 2

    spinner._stopped = StopEvent()
    monkeypatch.setattr(cli.sys, "stderr", type("Stderr", (), {"write": lambda *_args: None, "flush": lambda *_args: None})())

    spinner._spin()

    assert len(waits) == 2


def test_stream_run_does_not_repeat_the_final_content_snapshot(monkeypatch, capsys):
    monkeypatch.setattr(cli, "urlopen", lambda *_args, **_kwargs: _ContentThenDoneResponse())

    assert cli.stream_run("http://engine.test", "/api/threads/thread-1/runs") == 0

    output = capsys.readouterr().out
    assert "• OpenEngine\n  Working answer" in output
    assert output.count("Working answer") == 1


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


def test_runner_choice_labels_use_the_service_config_and_mark_the_default(monkeypatch):
    monkeypatch.setattr(cli, "fetch_json", lambda *_args: {
        "defaultRunner": "codex",
        "runners": [{"id": "codex"}, {"id": "claude"}],
    })

    assert cli.runner_choice_labels("http://engine.test") == {
        "Codex (default)": "codex", "Claude": "claude",
    }


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


def test_doctor_reports_prerequisites_and_keeps_a_stable_exit_code(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv(cli.CONFIG_ENVIRONMENT_VARIABLE, str(tmp_path / "config" / "cli.json"))
    monkeypatch.setattr(cli, "user_data_path", lambda _name: tmp_path / "data")
    def response(request, **_kwargs):
        url = request.full_url if hasattr(request, "full_url") else request
        if url.endswith("/api/source-control/status"):
            return _Response({"provider": "gh-cli"})
        return _Response({"service": "openengine", "version": "1.2.3", "ready": True, "api_version": 1})

    monkeypatch.setattr(cli, "urlopen", response)
    monkeypatch.setattr(daemon, "urlopen", response)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/tools/{name}")

    assert cli.main(["doctor", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert {check["name"] for check in report["checks"]} == {
        "service", "git", "codex", "claude", "source_control", "config", "data",
    }


def test_default_local_service_starts_the_registered_daemon(monkeypatch):
    unavailable = cli.Check("service", False, "cannot reach local service")
    ready = cli.Check("service", True, "OpenEngine 1.2.3 is ready")
    responses = iter([(unavailable, None), (ready, {"service": "openengine"})])
    monkeypatch.setattr(cli, "probe", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(daemon, "read_record", lambda: object())
    monkeypatch.setattr(daemon, "start_service", lambda: ("ready", {}, cli.DEFAULT_SERVER))
    monkeypatch.setattr(cli, "launch_local_service", lambda _server: (_ for _ in ()).throw(AssertionError("untracked launch")))

    check, _identity, launched = cli.ensure_service(cli.DEFAULT_SERVER)

    assert check.ok and launched


# --- engine review -------------------------------------------------------------

REVIEW_CONFIG = {
    "repositories": [{"name": "Owner/Repo", "path": "/code/repo"}],
    "workflows": [{
        "id": "implementation-review-rerank",
        "inputs": [
            {"name": "mode"}, {"name": "ref"}, {"name": "pr_url"}, {"name": "branch"},
            {"name": "state", "choices": ["Planning", "Review"]},
        ],
    }],
}
REVIEW_FINDINGS = [
    {"tagline": "The loop never ends", "description": "It skips the exit.", "file": "a.py", "line": 3, "facet": "bugs", "agent": "codex"},
    {"tagline": "Unused helper", "description": "Nothing calls it.", "facet": "conciseness", "agent": "codex"},
]
TRIAGE = {"approvalId": "approval-1", "nodeId": "triage", "toolName": "findings_triage"}


def test_review_of_a_pull_request_checks_out_its_branch_in_the_configured_repository(monkeypatch):
    monkeypatch.setattr(cli, "gh_json", lambda *_args, **_kwargs: {
        "url": "https://github.com/owner/repo/pull/7", "title": "Fix it",
        "headRefName": "patch-1", "isCrossRepository": False,
    })

    target = cli.review_target("https://github.com/owner/repo/pull/7/files", cli.DEFAULT_SERVER, REVIEW_CONFIG)

    assert target == cli.ReviewTarget(
        "/code/repo", "origin/patch-1", "https://github.com/owner/repo/pull/7",
        "Review pull request https://github.com/owner/repo/pull/7: Fix it", "patch-1",
    )


def test_review_of_a_fork_pull_request_is_refused(monkeypatch):
    monkeypatch.setattr(cli, "gh_json", lambda *_args, **_kwargs: {
        "url": "https://github.com/owner/repo/pull/7", "headRefName": "patch-1", "isCrossRepository": True,
    })

    with pytest.raises(RuntimeError, match="forks are not reviewed"):
        cli.review_target("https://github.com/owner/repo/pull/7", cli.DEFAULT_SERVER, REVIEW_CONFIG)


def test_review_does_not_take_a_similarly_named_checkout_for_the_pull_requests_repository(monkeypatch):
    remotes = {"https://github.com/myowner/repo.git": False, "git@github.com:Owner/Repo.git": True}
    request = cli.pull_request("https://github.com/owner/repo/pull/7")
    for origin, matches in remotes.items():
        monkeypatch.setattr(cli, "git_output", lambda _path, *arguments: origin if "get-url" in arguments else "/here")
        if matches:
            assert cli.review_repository(request, cli.DEFAULT_SERVER, {}) == "/here"
        else:
            with pytest.raises(RuntimeError, match="no checkout of owner/repo"):
                cli.review_repository(request, cli.DEFAULT_SERVER, {})


def test_review_starts_the_workflow_in_the_review_state(monkeypatch):
    posted = []
    monkeypatch.setattr(cli, "request_json", lambda _server, path, body: posted.append((path, body)) or {"runId": "run-1"})

    run_id = cli.start_review(cli.DEFAULT_SERVER, REVIEW_CONFIG, cli.ReviewTarget("/code/repo", "abc123", "", "Review it"))

    assert run_id == "run-1"
    assert posted == [("/api/runs", {
        "prompt": "Review it", "repository": "/code/repo", "workflowId": "implementation-review-rerank",
        # No pull request: nothing to push to, so the run stays off the forge.
        "inputs": {"state": "Review", "ref": "abc123", "pr_url": "", "branch": "", "mode": "disconnected"},
    })]


def test_review_of_a_pull_request_is_connected_to_its_branch(monkeypatch):
    posted = []
    monkeypatch.setattr(cli, "request_json", lambda _server, _path, body: posted.append(body) or {"runId": "run-1"})
    target = cli.ReviewTarget("/code/repo", "origin/patch-1", "https://github.com/o/r/pull/1", "Review it", "patch-1")

    cli.start_review(cli.DEFAULT_SERVER, REVIEW_CONFIG, target)

    assert posted[0]["inputs"]["branch"] == "patch-1"
    assert posted[0]["inputs"]["mode"] == "connected"


def test_review_json_waits_for_triage_and_prints_the_surviving_findings(monkeypatch, capsys):
    ready = cli.Check("service", True, "ready")
    monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, ready))
    monkeypatch.setattr(cli, "review_target", lambda *_args: cli.ReviewTarget("/code/repo", "abc", "https://github.com/o/r/pull/1", "Review"))
    monkeypatch.setattr(cli, "request_json", lambda *_args: {"runId": "run-1"})
    monkeypatch.setattr(cli, "REVIEW_POLL_SECONDS", 0)
    runs = iter([
        {"status": "running", "pendingApprovals": []},
        {"status": "awaiting_approval", "graphId": "g", "pendingApprovals": [TRIAGE], "values": {"review": REVIEW_FINDINGS}},
    ])
    monkeypatch.setattr(cli, "fetch_json", lambda _server, path: (
        REVIEW_CONFIG if path == "/api/config"
        else {"nodes": [{"nodeId": "triage", "findingsKey": "review"}]} if path == "/graph/api/graphs/g"
        else next(runs)
    ))

    assert cli.main(["review", "https://github.com/o/r/pull/1", "--json"]) == cli.EXIT_OK

    printed = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert printed == {"runId": "run-1", "prUrl": "https://github.com/o/r/pull/1", "findings": REVIEW_FINDINGS}


def test_review_says_each_step_as_it_starts_and_finishes(monkeypatch, capsys):
    monkeypatch.setattr(cli, "REVIEW_POLL_SECONDS", 0)
    runs = iter([
        {"status": "running", "graphId": "g", "pendingApprovals": [], "activeExecutions": [{"executionId": "e1", "nodeId": "implementation"}]},
        {"status": "running", "graphId": "g", "pendingApprovals": [], "activeExecutions": [
            {"executionId": "e2", "nodeId": "review_bugs"}, {"executionId": "e3", "nodeId": "unnamed"},
        ]},
        {"status": "awaiting_approval", "graphId": "g", "pendingApprovals": [TRIAGE], "activeExecutions": []},
    ])
    monkeypatch.setattr(cli, "fetch_json", lambda _server, path: (
        {"nodes": [{"nodeId": "implementation", "name": "Implementation"}, {"nodeId": "review_bugs", "name": "Review (Bugs)"}]}
        if path == "/graph/api/graphs/g" else next(runs)
    ))

    _run, triage = cli.wait_for_triage(cli.DEFAULT_SERVER, "run-1")

    assert triage == TRIAGE
    assert capsys.readouterr().err.splitlines() == [
        "→ Implementation", "✓ Implementation", "→ Review (Bugs)", "→ unnamed", "✓ Review (Bugs)", "✓ unnamed",
    ]


def test_review_retries_step_names_after_a_failed_fetch(monkeypatch, capsys):
    monkeypatch.setattr(cli, "REVIEW_POLL_SECONDS", 0)
    runs = iter([
        {"status": "running", "graphId": "g", "pendingApprovals": [], "activeExecutions": []},
        {"status": "running", "graphId": "g", "pendingApprovals": [], "activeExecutions": [{"executionId": "e1", "nodeId": "review_bugs"}]},
        {"status": "awaiting_approval", "graphId": "g", "pendingApprovals": [TRIAGE], "activeExecutions": []},
    ])
    graphs = iter([RuntimeError("blip"), {"nodes": [{"nodeId": "review_bugs", "name": "Review (Bugs)"}]}])

    def fetch(_server, path):
        if path != "/graph/api/graphs/g":
            return next(runs)
        graph = next(graphs)
        if isinstance(graph, Exception):
            raise graph
        return graph

    monkeypatch.setattr(cli, "fetch_json", fetch)

    cli.wait_for_triage(cli.DEFAULT_SERVER, "run-1")

    assert capsys.readouterr().err.splitlines() == ["→ Review (Bugs)", "✓ Review (Bugs)"]


def test_findings_are_all_reviewed_before_selected_fixes_are_sent(monkeypatch, capsys):
    offered, sent, posted = [], [], []
    findings = [*REVIEW_FINDINGS, {"tagline": "Third", "description": "Another issue."}]
    choices = iter(["Fix", "Post to PR", "Ignore"])

    def choose(options, _prompt, **kwargs):
        assert not sent, "implementation must wait until every finding is reviewed"
        offered.append(list(options))
        return next(choices)

    monkeypatch.setattr(cli, "palette", choose)
    monkeypatch.setattr(cli, "request_json", lambda _server, path, body: sent.append((path, body)) or {})
    monkeypatch.setattr(cli, "post_findings", lambda url, items: posted.append((url, items)) or 0)
    url = "https://github.com/o/r/pull/1"
    assert cli.choose_fixes(cli.DEFAULT_SERVER, "run-1", TRIAGE, findings, url, REVIEW_PATCH)
    assert offered == [["Fix", "Post to PR", "Ignore"]] * 3
    assert posted == [(url, [findings[1]])]
    assert sent == [
        ("/graph/api/runs/run-1/steering", {"message": json.dumps([findings[0]]), "node": "triage"}),
        ("/graph/api/runs/run-1/approvals/approval-1", {"decision": "accept"}),
    ]
    output = capsys.readouterr().out
    assert "Finding 2 of 3 · 1 queued for fixing" in output
    assert "Reviewed 3 of 3 findings" in output
    assert "+new" in output
    assert output.index("+new") < output.index(findings[0]["tagline"])
    assert "+new\n end\n\nFindings (1)" in output
    assert "unrelated" not in output


REVIEW_PATCH = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,3 +1,3 @@
 context
-old
+new
 end
@@ -20 +20 @@
-unrelated old
+unrelated new
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-other old
+other new
"""


def test_finding_diff_selects_file_and_hunk():
    patch = cli.finding_diff(REVIEW_PATCH, REVIEW_FINDINGS[0])
    assert "+new" in patch
    assert "unrelated" not in patch and "b/b.py" not in patch
    assert "unrelated" in cli.finding_diff(REVIEW_PATCH, {"file": "a.py"})
    assert cli.finding_diff(REVIEW_PATCH, {"file": "missing.py"}) == ""
    assert cli.finding_diff(REVIEW_PATCH, {}) == ""
    quoted = 'diff --git "a/a b.py" "b/a b.py"\n--- "a/a b.py"\n+++ "b/a b.py"\n@@ -1 +1 @@\n-old\n+new\n'
    assert cli.finding_diff(quoted, {"file": "a b.py", "line": 1}) == quoted


def test_diff_colors_only_on_a_terminal(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    cli.render_finding_diff("@@ -1 +1 @@\n-old\n+new")
    output = capsys.readouterr().out
    assert "\x1b[31m-old" in output and "\x1b[32m+new" in output
    monkeypatch.setenv("NO_COLOR", "1")
    cli.render_finding_diff("+new")
    assert capsys.readouterr().out == "+new\n"


@pytest.mark.parametrize("choices,decision", [(["Ignore", "Ignore"], "cancel"), (["Fix", None], None)])
def test_no_fixes_or_detaching_does_not_start_implementation(monkeypatch, choices, decision):
    choices = iter(choices)
    sent = []

    def choose(options, *_args, **_kwargs):
        assert "Post to PR" not in options
        return next(choices)

    monkeypatch.setattr(cli, "palette", choose)
    monkeypatch.setattr(cli, "request_json", lambda _server, path, body: sent.append(body) or {})
    assert not cli.choose_fixes(cli.DEFAULT_SERVER, "run-1", TRIAGE, REVIEW_FINDINGS, "")
    assert sent == ([{"decision": decision}] if decision else [])


@pytest.mark.parametrize("tty,no_color", [(True, False), (True, True), (False, False)])
def test_diff_escapes_terminal_controls_before_adding_color(monkeypatch, capsys, tty, no_color):
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: tty)
    monkeypatch.delenv("NO_COLOR", raising=False)
    if no_color:
        monkeypatch.setenv("NO_COLOR", "1")
    controls = [code for code in range(0xa0) if code < 32 or 127 <= code <= 159]
    controls = [code for code in controls if code not in (9, 10)]
    payload = "\x1b[2J\x1b]52;c;clipboard\x07\x9b2K\rhidden\b"
    line = "+\t" + "".join(map(chr, controls)) + payload
    cli.render_finding_diff(line + "\n context\n")
    expected = "+\t" + "".join(f"\\x{code:02x}" for code in controls)
    expected += "\\x1b[2J\\x1b]52;c;clipboard\\x07\\x9b2K\\x0dhidden\\x08"
    if tty and not no_color:
        expected = f"\x1b[32m{expected}\x1b[0m"
    assert capsys.readouterr().out == expected + "\n context\n"


@pytest.mark.parametrize("failure_stage", ["lookup", "posting"])
@pytest.mark.parametrize("next_action", ["Post to PR", "Fix", "Ignore"])
def test_post_errors_preserve_queued_fixes(monkeypatch, capsys, failure_stage, next_action):
    from types import SimpleNamespace

    choices = iter(["Fix", "Post to PR", next_action])
    sent = []
    failed = False

    def fail_once(stage):
        nonlocal failed
        if stage == failure_stage and not failed:
            failed = True
            raise RuntimeError("lookup unavailable") if stage == "lookup" else OSError("posting unavailable")

    def lookup(*_args):
        fail_once("lookup")
        return {"headRefOid": "sha"}

    def post(*_args, **_kwargs):
        fail_once("posting")
        return SimpleNamespace(returncode=0)

    def choose(*_args, **_kwargs):
        assert not sent
        return next(choices)

    monkeypatch.setattr(cli, "palette", choose)
    monkeypatch.setattr(cli, "gh_json", lookup)
    monkeypatch.setattr(cli.subprocess, "run", post)
    monkeypatch.setattr(cli, "request_json", lambda _server, path, body: sent.append((path, body)) or {})
    assert cli.choose_fixes(cli.DEFAULT_SERVER, "run-1", TRIAGE, REVIEW_FINDINGS, "https://github.com/o/r/pull/1")
    selected = REVIEW_FINDINGS if next_action == "Fix" else REVIEW_FINDINGS[:1]
    assert sent == [
        ("/graph/api/runs/run-1/steering", {"message": json.dumps(selected), "node": "triage"}),
        ("/graph/api/runs/run-1/approvals/approval-1", {"decision": "accept"}),
    ]
    output = capsys.readouterr()
    assert "Posting failed. Retry or choose another action." in output.out
    assert f"{failure_stage} unavailable" in output.err


def test_failed_post_keeps_the_finding_open(monkeypatch):
    choices = iter(["Post to PR", "Ignore", "Ignore"])
    posted = []
    monkeypatch.setattr(cli, "palette", lambda *_args, **_kwargs: next(choices))
    monkeypatch.setattr(cli, "post_findings", lambda _url, items: posted.append(items) or 1)
    monkeypatch.setattr(cli, "request_json", lambda *_args: {})
    assert not cli.choose_fixes(cli.DEFAULT_SERVER, "run-1", TRIAGE, REVIEW_FINDINGS, "https://github.com/o/r/pull/1")
    assert posted == [[REVIEW_FINDINGS[0]]]


def test_review_diff_reads_the_fixed_workspace_in_each_round(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "git_output", lambda *args: calls.append(args) or REVIEW_PATCH)
    target = cli.ReviewTarget("/repo", "old-commit", "", "Review")
    assert cli.review_diff(cli.DEFAULT_SERVER, {"values": {"workspace": "/review-checkout"}}, target) == REVIEW_PATCH
    assert calls == [("/review-checkout", "diff", "--no-ext-diff", "--no-textconv", "--no-color", "origin/HEAD...HEAD", "--")]


def test_review_diff_reads_pull_request_patch(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0, stdout=REVIEW_PATCH))
    target = cli.ReviewTarget("/remote/repo", "origin/topic", "https://github.com/o/r/pull/1", "Review")
    assert cli.review_diff("https://remote", {}, target) == REVIEW_PATCH
    assert calls == [["gh", "pr", "diff", target.pr_url, "--color=never"]]


def test_posting_findings_comments_inline_where_a_finding_has_a_line(monkeypatch):
    monkeypatch.setattr(cli, "gh_json", lambda *_args, **_kwargs: {"headRefOid": "sha"})
    commands = []

    class Completed:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda command, **_kwargs: commands.append(command) or Completed())

    assert cli.post_findings("https://github.com/o/r/pull/1", REVIEW_FINDINGS) == 0

    inline, general = commands
    assert inline[:7] == ["gh", "api", "--hostname", "github.com", "--method", "POST", "repos/o/r/pulls/1/comments"]
    assert "path=a.py" in inline and "line=3" in inline and "commit_id=sha" in inline
    assert general[:4] == ["gh", "pr", "comment", "https://github.com/o/r/pull/1"]
    assert general[-1].startswith("**Unused helper**")


def test_posting_a_finding_whose_line_is_not_a_number_comments_generally(monkeypatch):
    monkeypatch.setattr(cli, "gh_json", lambda *_args, **_kwargs: {"headRefOid": "sha"})
    commands = []

    class Completed:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda command, **_kwargs: commands.append(command) or Completed())
    # `gh -F line=@...` would read and send a local file.
    finding = {**REVIEW_FINDINGS[0], "line": "@~/.config/gh/hosts.yml"}

    assert cli.post_findings("https://github.com/o/r/pull/1", [finding]) == 0

    general, = commands
    assert general[:3] == ["gh", "pr", "comment"]
    assert not any("hosts.yml" in part for part in general)


@pytest.mark.parametrize("field", ["tagline", "description", "facet", "file", "line"])
def test_finding_fields_escape_terminal_controls(field, capsys):
    payload = "\x1b]52;c;clipboard\x07\x9b2K\rhidden\b"
    finding = {"file": "a.py", field: payload}
    original = dict(finding)
    cli.render_findings([finding])
    output = capsys.readouterr().out
    assert "\\x1b]52;c;clipboard\\x07\\x9b2K\\x0dhidden\\x08" in output
    # Only the renderer's own dim/reset sequences may reach the terminal.
    output = output.replace("\x1b[2m", "").replace("\x1b[0m", "")
    assert not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in output if char not in "\t\n")
    assert finding == original


@pytest.mark.parametrize("stage", ["lookup", "posting", "diff", "git"])
def test_review_errors_escape_command_stderr(monkeypatch, capsys, stage):
    from types import SimpleNamespace

    payload = "failure\x1b]52;c;clipboard\x07\x9b2K\rhidden\b"
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/bin/gh")
    monkeypatch.setattr(cli.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr=payload))
    if stage in ("lookup", "posting"):
        if stage == "posting":
            monkeypatch.setattr(cli, "gh_json", lambda *_args: {"headRefOid": "sha"})
        choices = iter(["Post to PR", "Ignore"])
        monkeypatch.setattr(cli, "palette", lambda *_args, **_kwargs: next(choices))
        monkeypatch.setattr(cli, "request_json", lambda *_args: {})
        cli.choose_fixes(cli.DEFAULT_SERVER, "run-1", TRIAGE, REVIEW_FINDINGS[:1], "https://github.com/o/r/pull/1")
    else:
        monkeypatch.setattr(cli, "read_service", lambda *_args: (cli.DEFAULT_SERVER, cli.Check("service", True, "ready")))
        monkeypatch.setattr(cli, "fetch_json", lambda *_args: REVIEW_CONFIG)
        if stage == "git":
            monkeypatch.setattr(cli, "review_target", lambda *_args: cli.git_output("/repo", "status"))
        else:
            monkeypatch.setattr(cli, "review_target", lambda *_args: cli.ReviewTarget("/repo", "abc", "https://github.com/o/r/pull/1", "Review"))
            monkeypatch.setattr(cli, "start_review", lambda *_args: "run-1")
            monkeypatch.setattr(cli, "wait_for_triage", lambda *_args: ({}, TRIAGE))
            monkeypatch.setattr(cli, "triage_findings", lambda *_args: REVIEW_FINDINGS)
            monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
            monkeypatch.setattr(cli, "choose_fixes", lambda *_args: False)
        cli.review(SimpleNamespace(target="/repo", json=False), cli.Preferences())
    error = capsys.readouterr().err
    assert "failure\\x1b]52;c;clipboard\\x07\\x9b2K\\x0dhidden\\x08" in error
    assert not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in error if char not in "\t\n")

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

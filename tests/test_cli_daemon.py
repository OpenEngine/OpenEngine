"""`engine daemon`: the service definitions it writes and its start/stop/status lifecycle."""

from __future__ import annotations

import json
import plistlib
import socket
import sys
from pathlib import Path

import pytest

from engine.apps.cli import __main__ as cli
from engine.apps.cli import daemon


@pytest.fixture
def home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ENGINE_CONFIG", raising=False)
    return tmp_path


def _spec(tmp_path: Path, **overrides) -> daemon.ServiceSpec:
    values = {
        "program": "/opt/openengine/venv/bin/engine-web",
        "config": str(tmp_path / "config" / "openengine" / "engine.toml"),
        "port": 4364,
        "log": str(tmp_path / "state" / "openengine" / "logs" / "engine-web.log"),
        "tools": {"git": "/usr/bin/git", "node": "/opt/node/bin/node", "npx": "/opt/node/bin/npx"},
    }
    return daemon.ServiceSpec(**{**values, **overrides})


def test_service_environment_uses_recorded_tools_and_loopback(tmp_path: Path):
    environment = _spec(tmp_path).environment()

    assert environment["ENGINE_CONFIG"] == str(tmp_path / "config" / "openengine" / "engine.toml")
    assert environment["ENGINE_HOST"] == "127.0.0.1"
    assert environment["PATH"].split(":")[:2] == ["/usr/bin", "/opt/node/bin"]
    assert "/usr/local/bin" in environment["PATH"].split(":")


def test_launch_agent_runs_engine_web_with_the_config_and_logs(tmp_path: Path):
    spec = _spec(tmp_path)

    plist = plistlib.loads(daemon.render_launch_agent(spec))

    assert plist["Label"] == daemon.LABEL
    assert plist["ProgramArguments"] == ["/opt/openengine/venv/bin/engine-web"]
    assert plist["EnvironmentVariables"] == spec.environment()
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == spec.log
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ExitTimeOut"] == daemon.STOP_TIMEOUT_SECONDS
    assert "engine-orchestrator" not in daemon.render_launch_agent(spec).decode()


def test_systemd_unit_runs_engine_web_and_stops_it_gracefully(tmp_path: Path):
    spec = _spec(tmp_path, program="/home/me/open engine/engine-web", tools={"git": "/usr/bin/git"})

    unit = daemon.render_systemd_unit(spec)

    assert 'ExecStart="/home/me/open engine/engine-web"\n' in unit
    assert f'Environment="ENGINE_CONFIG={spec.config}"\n' in unit
    assert 'Environment="ENGINE_HOST=127.0.0.1"\n' in unit
    assert 'Environment="PATH=/usr/bin:/usr/local/bin:/bin:/usr/sbin:/sbin"\n' in unit
    assert f"StandardOutput=append:{spec.log}\n" in unit
    assert "KillSignal=SIGTERM\n" in unit
    assert "TimeoutStopSec=30\n" in unit
    assert "WantedBy=default.target\n" in unit
    assert "orchestrator" not in unit


def test_paths_follow_the_installer_layout(home: Path):
    assert daemon.config_path() == home / "config" / "openengine" / "engine.toml"
    assert daemon.log_path() == home / "state" / "openengine" / "logs" / "engine-web.log"
    assert daemon.systemd_unit_path() == home / "config" / "systemd" / "user" / "openengine.service"
    assert daemon.launch_agent_path() == home / "Library" / "LaunchAgents" / f"{daemon.LABEL}.plist"


def test_setup_records_tools_and_falls_back_to_a_detached_process(home: Path, monkeypatch, capsys):
    config = home / "config" / "openengine" / "engine.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[server]\nport = 4411\n")
    monkeypatch.setattr(daemon, "engine_web_executable", lambda: Path("/venv/bin/engine-web"))
    monkeypatch.setattr(daemon.shutil, "which", lambda name: f"/tools/{name}" if name in {"git", "node", "npx"} else None)
    monkeypatch.setattr(daemon.LaunchdBackend, "available", classmethod(lambda _cls: False))
    monkeypatch.setattr(daemon.SystemdBackend, "available", staticmethod(lambda: False))
    started = []
    monkeypatch.setattr(daemon.ProcessBackend, "start", lambda _self, spec: started.append(spec))
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: ("down", None))
    monkeypatch.setattr(daemon, "start_service", lambda: ("ready", {"version": "1.0"}, "http://127.0.0.1:4411"))
    monkeypatch.setattr(daemon.webbrowser, "open", lambda _url: (_ for _ in ()).throw(AssertionError("no browser")))

    assert cli.main(["daemon", "setup", "--no-browser"]) == 0

    record = daemon.read_record()
    assert record is not None and record.backend == "process"
    assert record.spec.port == 4411
    assert record.spec.program == "/venv/bin/engine-web"
    assert record.spec.tools == {"git": "/tools/git", "node": "/tools/node", "npx": "/tools/npx"}
    assert started == [record.spec]
    assert "claude: not found" in capsys.readouterr().out


def test_doctor_reports_config_port_and_tools_without_requiring_an_agent(home: Path, monkeypatch, capsys):
    config = home / "config" / "openengine" / "engine.toml"
    config.parent.mkdir(parents=True)
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    config.write_text(f"[server]\nport = {port}\n")
    monkeypatch.setattr(daemon, "engine_web_executable", lambda: Path("/venv/bin/engine-web"))
    monkeypatch.setattr(daemon.shutil, "which", lambda name: f"/tools/{name}" if name in {"git", "node", "npx"} else None)

    assert cli.main(["daemon", "doctor", "--json"]) == 0

    checks = {check["name"]: check for check in json.loads(capsys.readouterr().out)["checks"]}
    assert list(checks) == ["config", "port", "engine-web", "git", "node", "npx", "claude", "codex"]
    assert checks["port"]["level"] == "ok"
    assert checks["claude"]["level"] == checks["codex"]["level"] == "warn"


def test_doctor_fails_without_a_config(home: Path, monkeypatch, capsys):
    monkeypatch.setattr(daemon, "engine_web_executable", lambda: Path("/venv/bin/engine-web"))

    assert cli.main(["daemon", "doctor"]) == 1

    assert "error  config:" in capsys.readouterr().out


FAKE_SERVICE = """\
import json, os, signal, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"service": "openengine", "version": "9.9.9", "ready": True,
                           "api_version": 1, "host": os.environ["ENGINE_HOST"]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass

server = HTTPServer((os.environ["ENGINE_HOST"], int(sys.argv[0].rsplit("-", 1)[1])), Health)
def shut_down(*_args):
    print("graceful shutdown", flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, shut_down)
print("serving", flush=True)
server.serve_forever()
"""


def test_start_status_stop_lifecycle_with_a_detached_process(home: Path, monkeypatch, capsys):
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    program = home / f"fake-engine-web-{port}"
    program.write_text(f"#!{sys.executable}\n{FAKE_SERVICE}")
    program.chmod(0o755)
    config = home / "config" / "openengine" / "engine.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f"[server]\nport = {port}\n")
    spec = daemon.ServiceSpec(str(program), str(config), port, str(daemon.log_path()), {})
    daemon.prepare_directories()
    daemon.write_record(daemon.Record("process", spec))

    assert cli.main(["daemon", "status"]) == 1
    assert "health: down" in capsys.readouterr().out

    assert cli.main(["daemon", "start"]) == 0
    assert f"OpenEngine 9.9.9 is running at http://127.0.0.1:{port}" in capsys.readouterr().out
    pid = daemon.ProcessBackend.pid()
    assert pid is not None

    # A second start reuses the one running instance.
    assert cli.main(["daemon", "start"]) == 0
    assert daemon.ProcessBackend.pid() == pid
    capsys.readouterr()

    assert cli.main(["daemon", "status", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["health"] == "ready"
    assert report["version"] == "9.9.9"
    assert report["url"] == f"http://127.0.0.1:{port}"
    assert report["running"] is True

    assert cli.main(["daemon", "stop"]) == 0
    assert daemon.ProcessBackend.pid() is None
    assert not daemon.pidfile_path().exists()
    assert not daemon.process_alive(pid)
    assert "graceful shutdown" in daemon.log_path().read_text()

    capsys.readouterr()
    assert cli.main(["daemon", "status"]) == 1
    assert "health: down" in capsys.readouterr().out


def test_start_refuses_a_port_held_by_another_service(home: Path, monkeypatch, capsys):
    config = home / "config" / "openengine" / "engine.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[server]\nport = 4412\n")
    spec = daemon.ServiceSpec("/venv/bin/engine-web", str(config), 4412, str(daemon.log_path()), {})
    daemon.prepare_directories()
    daemon.write_record(daemon.Record("process", spec))
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: ("foreign", None))
    monkeypatch.setattr(daemon.ProcessBackend, "start", lambda *_args: (_ for _ in ()).throw(AssertionError("must not start")))

    assert cli.main(["daemon", "start"]) == 1

    assert "another program is using http://127.0.0.1:4412" in capsys.readouterr().err

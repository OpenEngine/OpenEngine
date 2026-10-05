"""`engine daemon`: the service definitions it writes and its start/stop/status lifecycle."""

from __future__ import annotations

import json
import os
import plistlib
import socket
import subprocess
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
    monkeypatch.setattr(daemon, "_node_version", lambda _node: (24, 21, 0))
    monkeypatch.setattr(daemon, "_node_installs", lambda: [])
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
    assert environment["USER"] == environment["LOGNAME"] == daemon.getpass.getuser()
    assert environment["PATH"].split(":")[:2] == ["/opt/node/bin", "/usr/bin"]
    assert "/usr/local/bin" in environment["PATH"].split(":")


def test_launch_agent_runs_engine_web_with_the_config_and_logs(tmp_path: Path):
    spec = _spec(tmp_path)

    plist = plistlib.loads(daemon.render_launch_agent(spec))

    assert plist["Label"] == daemon.LABEL
    assert plist["ProgramArguments"] == ["/opt/openengine/venv/bin/engine-web"]
    assert plist["EnvironmentVariables"] == spec.environment()
    assert plist["EnvironmentVariables"]["USER"] == daemon.getpass.getuser()
    assert plist["EnvironmentVariables"]["LOGNAME"] == daemon.getpass.getuser()
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == spec.log
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ExitTimeOut"] == daemon.STOP_TIMEOUT_SECONDS
    assert "engine-orchestrator" not in daemon.render_launch_agent(spec).decode()


def test_launchd_restart_waits_for_old_registration_to_disappear(home: Path, monkeypatch):
    # Model launchd after bootout: HTTP has closed, but print still succeeds
    # and kickstart is a successful no-op until teardown removes the job.
    registered = False
    stopping = False
    starts = 0
    sleeps = 0

    def run(command):
        nonlocal registered, stopping, starts
        action = command[1]
        code = 0
        if action == "print":
            code = 0 if registered else 113
        elif action == "bootstrap":
            assert not registered
            registered = True
            stopping = False
            starts += 1
        elif action == "bootout":
            stopping = True
        else:
            assert action == "kickstart"
        return subprocess.CompletedProcess(command, code, "", "")

    def sleep(_seconds):
        nonlocal sleeps, registered
        assert stopping
        sleeps += 1
        if sleeps == 2:
            registered = False

    monkeypatch.setattr(daemon, "_run", run)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    backend = daemon.LaunchdBackend()
    spec = _spec(home)
    backend.start(spec)
    backend.stop()
    backend.start(spec)

    assert starts == 2
    assert sleeps == 2


def test_launchd_stop_times_out_if_registration_remains(monkeypatch):
    now = 0.0

    def sleep(seconds):
        nonlocal now
        now += seconds

    monkeypatch.setattr(daemon, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", ""))
    monkeypatch.setattr(daemon.time, "monotonic", lambda: now)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    monkeypatch.setattr(daemon, "STOP_TIMEOUT_SECONDS", 1.0)

    with pytest.raises(RuntimeError, match="launchd did not remove"):
        daemon.LaunchdBackend().stop()

    assert 1.0 <= now < 1.3


@pytest.mark.parametrize("removed", [False, True])
def test_launchd_stop_reports_bootout_failure_unless_already_removed(monkeypatch, removed):
    checks = iter([True, not removed])
    monkeypatch.setattr(daemon.LaunchdBackend, "running", lambda _self: next(checks))
    monkeypatch.setattr(daemon, "_run", lambda command: subprocess.CompletedProcess(command, 5, "", "removal failed"))

    if removed:
        daemon.LaunchdBackend().stop()
    else:
        with pytest.raises(RuntimeError, match="launchctl bootout failed: removal failed"):
            daemon.LaunchdBackend().stop()


def test_systemd_unit_runs_engine_web_and_stops_it_gracefully(tmp_path: Path):
    spec = _spec(tmp_path, program="/home/me/open engine/engine-web", tools={"git": "/usr/bin/git"})

    unit = daemon.render_systemd_unit(spec)

    assert 'ExecStart="/home/me/open engine/engine-web"\n' in unit
    assert f'Environment="ENGINE_CONFIG={spec.config}"\n' in unit
    assert 'Environment="ENGINE_HOST=127.0.0.1"\n' in unit
    assert f'Environment="USER={daemon.getpass.getuser()}"\n' in unit
    assert f'Environment="LOGNAME={daemon.getpass.getuser()}"\n' in unit
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


def test_setup_unregisters_the_service_when_it_falls_back_to_a_process(home: Path, monkeypatch):
    config = home / "config" / "openengine" / "engine.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[server]\nport = 4413\n")
    monkeypatch.setattr(daemon, "engine_web_executable", lambda: Path("/venv/bin/engine-web"))
    monkeypatch.setattr(daemon.LaunchdBackend, "available", classmethod(lambda _cls: True))
    monkeypatch.setattr(daemon.LaunchdBackend, "running", lambda _self: False)
    monkeypatch.setattr(daemon.LaunchdBackend, "start",
                        lambda _self, _spec: (_ for _ in ()).throw(RuntimeError("launchctl bootstrap failed")))
    monkeypatch.setattr(daemon.ProcessBackend, "start", lambda _self, _spec: None)
    monkeypatch.setattr(daemon, "health", lambda _url, timeout=2.0: ("down", None))
    monkeypatch.setattr(daemon, "start_service", lambda: ("ready", {}, "http://127.0.0.1:4413"))

    assert cli.main(["daemon", "setup", "--no-browser"]) == 0

    assert daemon.read_record().backend == "process"
    assert not daemon.launch_agent_path().exists()


def test_commands_follow_a_port_edited_after_setup(home: Path):
    config = home / "config" / "openengine" / "engine.toml"
    config.parent.mkdir(parents=True)
    config.write_text("[server]\nport = 4414\n")
    daemon.prepare_directories()
    daemon.write_record(daemon.Record("process", daemon.ServiceSpec("/venv/bin/engine-web", str(config), 4414, str(daemon.log_path()), {})))

    config.write_text("[server]\nport = 4415\n")

    assert daemon.current()[1].url == "http://127.0.0.1:4415"


def test_stop_leaves_alone_a_process_that_reused_the_recorded_pid(home: Path):
    daemon.prepare_directories()
    daemon.pidfile_path().write_text(f"{os.getpid()}\n/venv/bin/engine-web\n")

    daemon.ProcessBackend().stop()

    assert daemon.process_alive(os.getpid())
    assert not daemon.pidfile_path().exists()


def test_tail_reads_only_the_last_lines(tmp_path: Path):
    log = tmp_path / "engine-web.log"
    log.write_bytes(b"".join(f"line {number}\n".encode() for number in range(1000)))

    with log.open("rb") as file:
        assert daemon.tail(file, 3, block=16) == [b"line 997\n", b"line 998\n", b"line 999\n"]
        assert file.tell() == log.stat().st_size
        assert daemon.tail(file, 0) == []
        assert len(daemon.tail(file, 5000)) == 1000


def test_start_rotates_an_oversized_log(home: Path, monkeypatch):
    daemon.prepare_directories()
    log = daemon.log_path()
    log.write_bytes(b"x" * 11)
    monkeypatch.setattr(daemon, "LOG_ROTATE_BYTES", 10)

    daemon.rotate_log(log)

    assert not log.exists()
    assert log.with_name("engine-web.log.1").read_bytes() == b"x" * 11


@pytest.mark.parametrize(
    "returncode, output, level",
    [
        (0, '{"loggedIn": true, "email": "work@example.com"}', "ok"),
        (1, '{"loggedIn": false}', "warn"),
        (0, 'not JSON', "warn"),
        (0, '[]', "warn"),
    ],
)
@pytest.mark.parametrize("configured", [True, False])
def test_doctor_checks_service_claude_login(home, monkeypatch, returncode, output, level, configured):
    spec = _spec(home, tools={"claude": "/recorded/bin/claude", "node": "/node/bin/node"})
    config = Path(spec.config)
    config.parent.mkdir(parents=True)
    account = config.parent / "account"
    account.mkdir()
    config.write_text('[claude]\nconfig_dir = "account"\n' if configured else "")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/shell/account")
    monkeypatch.setattr(daemon, "read_record", lambda: daemon.Record(backend="process", spec=spec))
    monkeypatch.setattr(daemon, "detect_tools", lambda: {"claude": "/shell/bin/claude"})
    monkeypatch.setattr(daemon, "engine_web_executable", lambda: Path(spec.program))
    monkeypatch.setattr(daemon, "_port_finding", lambda port: daemon.Finding("port", "ok", "free"))

    def run(command, **kwargs):
        assert command == ["/recorded/bin/claude", "auth", "status"]
        env = kwargs["env"]
        assert env["USER"] == env["LOGNAME"] == daemon.getpass.getuser()
        assert env["PATH"].split(os.pathsep)[:2] == ["/recorded/bin", "/node/bin"]
        assert env.get("CLAUDE_CONFIG_DIR") == (str(account) if configured else None)
        return subprocess.CompletedProcess(command, returncode, output, "")

    monkeypatch.setattr(daemon.subprocess, "run", run)
    finding = next(f for f in daemon.diagnose() if f.name == "claude login")
    assert finding.level == level
    expected = account if configured else home / ".claude"
    assert str(expected) in finding.detail
    assert ("work@example.com" if level == "ok" else "run claude /login with CLAUDE_CONFIG_DIR=") in finding.detail


@pytest.mark.parametrize("error", [OSError("not executable"), subprocess.TimeoutExpired("claude", 15)])
def test_doctor_claude_auth_failure_is_a_warning(home, monkeypatch, error):
    spec = _spec(home, tools={"claude": "/recorded/bin/claude"})
    config = Path(spec.config)
    config.parent.mkdir(parents=True)
    config.write_text("")

    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(daemon.subprocess, "run", run)
    assert daemon._claude_login_finding(spec).level == "warn"


@pytest.mark.parametrize('version', [(24, 21, 0), None, (20, 18, 0)])
def test_detect_node(home, monkeypatch, capsys, version):
    monkeypatch.setattr(daemon.shutil, 'which', lambda name: f'/shims/{name}')
    monkeypatch.setattr(daemon, '_node_version', lambda node: (26, 10, 0) if node == '/real/bin/node' else version)
    monkeypatch.setattr(daemon, '_node_installs', lambda: [Path('/real/bin')] if version is None else [])
    tools = daemon.detect_tools()
    directory = '/real/bin' if version is None else '/shims'
    assert tools['node'] == directory + '/node'
    assert tools['npx'] == directory + '/npx'
    output = capsys.readouterr().err
    if version == (24, 21, 0):
        assert not output
    elif version:
        assert '20.18.0' in output and daemon.NODE_HELP in output
    else:
        assert 'cannot run outside a project; degraded: using /real/bin/node' in output


def test_node_installs_across_managers(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for variable, directory in [('XDG_DATA_HOME', 'data'), ('MISE_DATA_DIR', 'mise'),
                                ('ASDF_DATA_DIR', 'asdf'), ('NVM_DIR', 'nvm')]:
        monkeypatch.setenv(variable, str(tmp_path / directory))
    paths = ['mise/installs/node/26.1.0/bin', 'asdf/installs/nodejs/24.1.0/bin',
             'nvm/versions/node/v22.1.0/bin', '.nodenv/versions/23.1.0/bin',
             '.volta/tools/image/node/21.1.0/bin', 'data/fnm/node-versions/v25.1.0/installation/bin',
             'Library/Application Support/fnm/node-versions/v20.19.0/installation/bin',
             'mise/installs/node/28.1.0/bin', 'mise/installs/node/20.18.0/bin',
             'mise/installs/node/27.1.0/bin']
    versions = {}
    for path in paths:
        directory = tmp_path / path
        directory.mkdir(parents=True)
        (directory / 'node').touch()
        if '27.1.0' not in path:
            (directory / 'npx').touch()
        release = directory.parent.name if directory.parent.name != 'installation' else directory.parent.parent.name
        versions[str(directory / 'node')] = None if release == '28.1.0' else tuple(map(int, release.lstrip('v').split('.')))
    root = tmp_path / 'mise/installs/node'
    for alias in ('latest', '26', '29.0.0'):
        (root / alias).symlink_to(root / '26.1.0', target_is_directory=True)
    monkeypatch.setattr(daemon, '_node_version', lambda node: versions.get(node))
    installs = daemon._node_installs()
    assert installs[:9] == [tmp_path / paths[i] for i in (7, 0, 5, 1, 3, 2, 4, 6, 8)]
    monkeypatch.setattr(daemon.shutil, 'which', lambda name: f'/shims/{name}')
    probes = []
    def probe(node):
        probes.append(node)
        return versions.get(node)
    monkeypatch.setattr(daemon, '_node_version', probe)
    check = daemon._check_node(daemon._path_tools())
    tools, problem = check.tools, check.problem
    assert tools['node'] == str(installs[1] / 'node')
    assert probes == ['/shims/node', str(installs[0] / 'node'), str(installs[1] / 'node')]
    assert problem



@pytest.mark.parametrize('healable', [True, False])
@pytest.mark.parametrize('recorded', [True, False])
def test_start_heals_node(home, monkeypatch, capsys, healable, recorded):
    spec = _spec(home, tools={'git': '/usr/bin/git', 'node': '/shims/node', 'npx': '/shims/npx'})
    Path(spec.config).parent.mkdir(parents=True)
    Path(spec.config).write_text('[server]\nport = 4364\n')
    daemon.prepare_directories()
    if recorded:
        daemon.write_record(daemon.Record('process', spec))
    monkeypatch.setattr(daemon, 'engine_web_executable', lambda: Path(spec.program))
    monkeypatch.setattr(daemon.shutil, 'which', lambda name: spec.tools.get(name))
    monkeypatch.setattr(daemon, '_node_version', lambda node: (26, 10, 0) if node == '/real/bin/node' else None)
    monkeypatch.setattr(daemon, '_node_installs', lambda: [Path('/real/bin')] if healable else [])
    monkeypatch.setattr(daemon, 'health', lambda _url: ('down', None))
    monkeypatch.setattr(daemon, 'wait_for', lambda *_args: ('ready', {}))
    events = []
    monkeypatch.setattr(daemon.ProcessBackend, 'stop', lambda _self: events.append('stop'))
    monkeypatch.setattr(daemon.ProcessBackend, 'install', lambda _self, spec: events.append(('install', spec)))
    monkeypatch.setattr(daemon.ProcessBackend, 'start', lambda _self, spec: events.append(('start', spec)))
    assert cli.main(['daemon', 'start']) == 0
    record = daemon.read_record()
    if healable:
        assert record is not None
        assert events == ['stop', ('install', record.spec), ('start', record.spec)]
        assert record.spec.tools['node'] == '/real/bin/node'
        assert record.spec.tools['npx'] == '/real/bin/npx'
        assert record.spec.environment()['PATH'].split(':')[:2] == ['/real/bin', '/shims']
        detail = 'degraded: using /real/bin/node'
    else:
        assert record == (daemon.Record('process', spec) if recorded else None)
        assert events == [('start', spec)]
        detail = f'warning: /shims/node cannot run outside a project; {daemon.NODE_HELP}'
    output = capsys.readouterr().err.splitlines()
    assert len(output) == 1
    assert detail in output[0]
    if healable:
        assert 'project Node pins are ignored while healed' in output[0]


@pytest.mark.parametrize('recorded', [True, False])
@pytest.mark.parametrize('healable', [True, False])
def test_doctor_probes_node(home, monkeypatch, capsys, recorded, healable):
    spec = _spec(home)
    Path(spec.config).parent.mkdir(parents=True)
    Path(spec.config).write_text('[server]\nport = 4364\n')
    if recorded:
        daemon.prepare_directories()
        daemon.write_record(daemon.Record('process', spec))
    monkeypatch.setattr(daemon, 'engine_web_executable', lambda: Path(spec.program))
    monkeypatch.setattr(daemon, '_port_finding', lambda _port: daemon.Finding('port', 'ok', 'free'))
    monkeypatch.setattr(daemon.shutil, 'which', lambda name: f'/shims/{name}')
    monkeypatch.setattr(daemon, '_node_version', lambda node: (26, 10, 0) if node == '/real/bin/node' else None)
    monkeypatch.setattr(daemon, '_node_installs', lambda: [Path('/real/bin')] if healable else [])
    assert cli.main(['daemon', 'doctor', '--json']) == (0 if healable else 1)
    nodes = [check for check in json.loads(capsys.readouterr().out)['checks'] if check['name'] == 'node']
    assert len(nodes) == 1
    assert nodes[0]['level'] == ('warn' if healable else 'error')
    assert ('engine daemon start will use /real/bin/node' if healable else daemon.NODE_HELP) in nodes[0]['detail']
    if healable:
        assert 'degraded' in nodes[0]['detail']
        assert 'project Node pins are ignored while healed' in nodes[0]['detail']


@pytest.mark.parametrize('result', ['v20.19.0', 'invalid', 'failure', 'timeout', 'missing'])
def test_node_probe_uses_home_and_timeout(tmp_path, monkeypatch, result):
    monkeypatch.setenv('HOME', str(tmp_path))

    def run(command, **kwargs):
        assert command == ['/shims/node', '--version']
        assert kwargs['cwd'] == tmp_path
        assert kwargs['timeout'] == 5
        if result == 'timeout':
            raise subprocess.TimeoutExpired(command, 5)
        if result == 'missing':
            raise FileNotFoundError()
        return subprocess.CompletedProcess(command, int(result == 'failure'), result, '')

    monkeypatch.setattr(daemon.subprocess, 'run', run)
    assert daemon._node_version('/shims/node') == ((20, 19, 0) if result == 'v20.19.0' else None)


@pytest.mark.parametrize("manager,command", [
    ("mise", "mise use -g node@26.10.0"),
    ("asdf", "asdf set -u nodejs 26.10.0"),
    ("nvm", "nvm alias default 26.10.0"),
    ("nodenv", "nodenv global 26.10.0"),
])
def test_healed_doctor_retains_degraded_state(home, monkeypatch, manager, command):
    tools = {"node": "/real/bin/node", "npx": "/real/bin/npx",
             "original_node": f"/{manager}/shims/node",
             "original_npx": f"/{manager}/shims/npx"}
    spec = _spec(home, tools=tools)
    Path(spec.config).parent.mkdir(parents=True)
    Path(spec.config).write_text("[server]\nport = 4364\n")
    daemon.prepare_directories()
    daemon.write_record(daemon.Record("process", spec))
    monkeypatch.setattr(daemon, "_port_finding", lambda _port: daemon.Finding("port", "ok", "free"))
    monkeypatch.setattr(daemon, "_node_version", lambda node: (26, 10, 0))
    node = next(f for f in daemon.diagnose() if f.name == "node")
    assert node.level == "warn"
    assert "degraded" in node.detail
    assert "project Node pins are ignored while healed" in node.detail
    assert command in node.detail
    assert "engine daemon setup" in node.detail


def test_working_shim_keeps_project_switching(home, monkeypatch):
    tools = {"git": "/usr/bin/git", "node": "/mise/shims/node", "npx": "/mise/shims/npx"}
    check = daemon._check_node(tools)
    checked, problem = check.tools, check.problem
    assert checked == tools
    assert problem is None
    assert _spec(home, tools=checked).environment()["PATH"].split(":")[0] == "/mise/shims"


def test_candidate_probe_skips_old_and_broken(home, monkeypatch):
    candidates = [Path("/broken/bin"), Path("/old/bin"), Path("/good/bin"), Path("/unused/bin")]
    monkeypatch.setattr(daemon, "_node_installs", lambda: candidates)
    versions = {"/old/bin/node": (20, 18, 0), "/good/bin/node": (20, 19, 0)}
    probes = []
    def probe(node):
        probes.append(node)
        return versions.get(node)
    monkeypatch.setattr(daemon, "_node_version", probe)
    check = daemon._check_node({"node": "/shims/node", "npx": "/shims/npx"})
    tools = check.tools
    assert tools["node"] == "/good/bin/node"
    assert probes == ["/shims/node", "/broken/bin/node", "/old/bin/node", "/good/bin/node"]


@pytest.mark.parametrize("operation", ["detect", "start", "doctor"])
@pytest.mark.parametrize("already_healed", [False, True])
def test_node_diagnostics_reuse_verified_version(home, monkeypatch, capsys, operation, already_healed):
    tools = {"node": "/mise/shims/node", "npx": "/mise/shims/npx"}
    if already_healed:
        tools = {**tools, "original_node": tools["node"], "original_npx": tools["npx"],
                 "node": "/real/bin/node", "npx": "/real/bin/npx"}
    spec = _spec(home, tools=tools)
    Path(spec.config).parent.mkdir(parents=True)
    Path(spec.config).write_text("[server]\nport = 4364\n")
    daemon.prepare_directories()
    daemon.write_record(daemon.Record("process", spec))
    probes = []

    def probe(node):
        probes.append(node)
        return (26, 10, 0) if node == "/real/bin/node" else None

    monkeypatch.setattr(daemon, "_node_version", probe)
    monkeypatch.setattr(daemon, "_node_installs", lambda: [Path("/real/bin"), Path("/unused/bin")])
    monkeypatch.setattr(daemon, "_path_tools", lambda: tools)
    monkeypatch.setattr(daemon, "engine_web_executable", lambda: Path(spec.program))
    monkeypatch.setattr(daemon, "_port_finding", lambda _port: daemon.Finding("port", "ok", "free"))
    monkeypatch.setattr(daemon, "health", lambda _url: ("down", None))
    monkeypatch.setattr(daemon, "wait_for", lambda *_args: ("ready", {}))
    for method in ("stop", "install", "start"):
        monkeypatch.setattr(daemon.ProcessBackend, method, lambda *_args: None)
    if operation == "detect":
        daemon.detect_tools()
        detail = capsys.readouterr().err
    elif operation == "start":
        daemon.start_service()
        detail = capsys.readouterr().err
    else:
        finding = next(f for f in daemon.diagnose() if f.name == "node")
        assert finding.level == "warn"
        detail = finding.detail
    assert probes == (["/real/bin/node"] if already_healed else ["/mise/shims/node", "/real/bin/node"])
    assert "degraded" in detail
    assert "project Node pins are ignored while healed" in detail
    assert "mise use -g node@26.10.0" in detail

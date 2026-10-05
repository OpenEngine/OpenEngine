"""Run the local OpenEngine web service in the background for the current user.

`engine daemon setup` records where node, npx, git and the agent CLIs live,
then registers a per-user LaunchAgent on macOS or a systemd user unit on
Linux. Where neither can be used, the service is a detached process tracked by
a pidfile. Either way the service is `engine-web` alone, bound to loopback,
with `ENGINE_CONFIG` set and a PATH built only from the recorded tool
directories, so it never depends on the interactive shell that ran setup.
Temporal is not used: `engine-orchestrator` is never started.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import plistlib
import re
import shutil
import signal
import shlex
import socket
import subprocess
import sys
import time
import tomllib
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from importlib.metadata import version
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from engine.runtime.config import load_engine_config

LABEL = "sh.openengine.engine"
SYSTEMD_UNIT = "openengine.service"
HOST = "127.0.0.1"
DEFAULT_PORT = 4364
TOOLS = ("git", "node", "npx", "claude", "codex")
# The service needs node and npx for the agent adapters and git for
# workspaces; an agent CLI is optional, because the UI opens without one.
REQUIRED_TOOLS = ("git", "node", "npx")
SYSTEM_PATH = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
START_TIMEOUT_SECONDS = 60.0
# The service appends to one log; keep one previous copy past this size.
LOG_ROTATE_BYTES = 10 * 1024 * 1024
STOP_TIMEOUT_SECONDS = 30.0
EXIT_OK = 0
EXIT_FAILED = 1


def _xdg(variable: str, default: str) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value and Path(value).is_absolute() else Path.home() / default


def config_path() -> Path:
    """The configuration the installer writes, unless ENGINE_CONFIG names another."""
    if value := os.environ.get("ENGINE_CONFIG"):
        return Path(value).expanduser().absolute()
    return _xdg("XDG_CONFIG_HOME", ".config") / "openengine" / "engine.toml"


def state_directory() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "openengine"


def log_directory() -> Path:
    return state_directory() / "logs"


def log_path() -> Path:
    return log_directory() / "engine-web.log"


def rotate_log(path: Path) -> None:
    """Keep the log bounded; call only while the service is down and not writing it."""
    try:
        if path.stat().st_size > LOG_ROTATE_BYTES:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def pidfile_path() -> Path:
    return state_directory() / "engine-web.pid"


def record_path() -> Path:
    return state_directory() / "daemon.json"


def launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def systemd_unit_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "systemd" / "user" / SYSTEMD_UNIT


def configured_port(path: Path) -> int:
    """The `[server] port` the service will bind, which the URL must match."""
    with path.open("rb") as file:
        server = tomllib.load(file).get("server", {})
    port = server.get("port", DEFAULT_PORT) if isinstance(server, dict) else DEFAULT_PORT
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port <= 65535:
        raise ValueError(f"[server] port in {path} must be an integer from 1 to 65535")
    return port


def service_url(port: int) -> str:
    return f"http://{HOST}:{port}"


def engine_web_executable() -> Path:
    """`engine-web` beside this interpreter, so the service runs this install."""
    sibling = Path(sys.executable).parent / "engine-web"
    if sibling.is_file():
        return sibling
    found = shutil.which("engine-web")
    if found is None:
        raise RuntimeError("engine-web is not installed alongside engine")
    return Path(found).absolute()


NODE_MINIMUM = (20, 19, 0)
NODE_HELP = "install Node 20.19+ or set a global default in your version manager"


def _node_version(node: str) -> tuple[int, int, int] | None:
    """Probe outside the invoking project, where an unconfigured shim fails."""
    try:
        result = subprocess.run(
            [node, "--version"], cwd=Path.home(), capture_output=True,
            text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", result.stdout.strip())
    if result.returncode != 0 or match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _node_manager_homes() -> list[tuple[str, Path]]:
    home = Path.home()
    data = _xdg("XDG_DATA_HOME", ".local/share")
    return [
        ("mise", Path(os.environ.get("MISE_DATA_DIR", data / "mise"))),
        ("asdf", Path(os.environ.get("ASDF_DATA_DIR", home / ".asdf"))),
        ("nvm", Path(os.environ.get("NVM_DIR", home / ".nvm"))),
        ("nodenv", home / ".nodenv"),
        ("volta", home / ".volta"),
        ("fnm", data / "fnm"),
        ("fnm", home / "Library/Application Support/fnm"),
    ]


def _shim_manager(node: str | None) -> str | None:
    if node:
        path = Path(node)
        for manager, home in _node_manager_homes():
            if path.parent == home / "shims":
                return manager
            # nvm/fnm expose an installation's bin directory rather than shims.
            if manager in {"nvm", "fnm"} and path.is_relative_to(home):
                return manager
            if manager == "volta" and path.parent == home / "bin":
                return manager
    return None


@dataclass(frozen=True)
class NodeInstall:
    directory: Path
    manager: str | None


def _node_installs() -> list[NodeInstall]:
    """Unprobed installs with provenance, newest first, then system fallbacks."""
    layouts = {
        "mise": ("installs/node", "*/bin"),
        "asdf": ("installs/nodejs", "*/bin"),
        "nvm": ("versions/node", "*/bin"),
        "nodenv": ("versions", "*/bin"),
        "volta": ("tools/image/node", "*/bin"),
        "fnm": ("node-versions", "*/installation/bin"),
    }
    candidates = []
    for manager, home in _node_manager_homes():
        suffix, pattern = layouts[manager]
        root = home / suffix
        for directory in root.glob(pattern):
            release = directory.relative_to(root).parts[0]
            if (root / release).is_symlink() or not re.fullmatch(r"v?\d+\.\d+\.\d+", release):
                continue
            if (directory / "npx").is_file():
                candidates.append((tuple(map(int, release.lstrip("v").split("."))), NodeInstall(directory, manager)))
    ordered = [directory for _, directory in sorted(candidates, key=lambda item: item[0], reverse=True)]
    ordered.extend(NodeInstall(Path(path), None) for path in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")
                   if (Path(path) / "npx").is_file())
    return ordered


@dataclass(frozen=True)
class NodeCheck:
    tools: dict[str, str]
    problem: str | None
    version: tuple[int, int, int] | None
    healed: bool = False
    shim_manager: str | None = None
    install_manager: str | None = None


def _check_node(tools: dict[str, str]) -> NodeCheck:
    node = tools.get("node")
    manager = _shim_manager(tools.get("original_node", node))
    version = _node_version(node) if node else None
    if version is not None and version >= NODE_MINIMUM:
        install_manager = next((item.manager for item in _node_installs()
                                if str(item.directory / "node") == node), None) if "original_node" in tools else None
        return NodeCheck(tools, "using a healed Node installation" if "original_node" in tools else None,
                         version, healed="original_node" in tools,
                         shim_manager=manager, install_manager=install_manager)
    if version is not None:
        problem = f"{node} is Node {'.'.join(map(str, version))}; requires Node 20.19+"
    else:
        problem = f"{node} cannot run outside a project" if node else "node not found"
    for install in _node_installs():
        directory = install.directory
        candidate_version = _node_version(str(directory / "node"))
        if candidate_version is None or candidate_version < NODE_MINIMUM:
            continue
        healed = dict(tools)
        for name in ("node", "npx"):
            if name in tools:
                healed.setdefault(f"original_{name}", tools[name])
            healed[name] = str(directory / name)
        return NodeCheck(healed, problem, candidate_version, healed=True,
                         shim_manager=manager, install_manager=install.manager)
    return NodeCheck(tools, problem, version)


def _healing_notice(check: NodeCheck, *, verbose: bool = False) -> str:
    """Format the verified result without spawning Node again."""
    release = ".".join(map(str, check.version)) if check.version else None
    commands = {
        "mise": f"mise use -g node@{release}",
        "asdf": f"asdf set -u nodejs {release}",
        "nvm": f"nvm alias default {release}",
        "nodenv": f"nodenv global {release}",
        "fnm": f"fnm default {release}",
    }
    command = commands.get(check.shim_manager) if release and check.shim_manager == check.install_manager else None
    manager = check.shim_manager or "your version manager"
    restore = f"run {command}" if command else f"set a global Node 20.19+ default in {manager}"
    notice = (f"degraded: using {check.tools['node']}; project Node pins are ignored until you "
              f"{restore}, then engine daemon setup")
    if verbose:
        notice += ("; healing is a fallback; the global default applies only where no project selects Node. "
                   "Run setup from a shell with the manager on PATH to restore project switching.")
        if command and check.shim_manager == "asdf":
            notice += f" For asdf <0.16, use asdf global nodejs {release}."
    return notice


def _path_tools() -> dict[str, str]:
    return {name: str(Path(found).absolute()) for name in TOOLS if (found := shutil.which(name))}


def detect_tools() -> dict[str, str]:
    found = _path_tools()
    check = _check_node(found)
    tools, problem = check.tools, check.problem
    if problem:
        detail = _healing_notice(check) if check.healed else NODE_HELP
        notice = detail if check.healed else f"{problem}; {detail}"
        print(f"engine daemon: {notice}", file=sys.stderr)
    return tools


@dataclass(frozen=True)
class ServiceSpec:
    """Everything a service manager needs, resolved to absolute paths."""

    program: str
    config: str
    port: int
    log: str
    tools: dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return service_url(self.port)

    def environment(self) -> dict[str, str]:
        directories: list[str] = []
        for name in ("node", "npx", "original_node", "original_npx",
                     *(name for name in self.tools if name not in {"node", "npx", "original_node", "original_npx"})):
            tool = self.tools.get(name)
            if tool is None:
                continue
            directory = str(Path(tool).parent)
            if directory not in directories:
                directories.append(directory)
        directories += [path for path in SYSTEM_PATH if path not in directories]
        user = getpass.getuser()
        return {
            "ENGINE_CONFIG": self.config,
            "ENGINE_HOST": HOST,
            "HOME": str(Path.home()),
            "USER": user,
            "LOGNAME": user,
            "PATH": os.pathsep.join(directories),
        }


def build_spec(tools: dict[str, str] | None = None) -> ServiceSpec:
    config = config_path()
    if not config.is_file():
        raise RuntimeError(f"no configuration at {config}; run the installer or set ENGINE_CONFIG")
    return ServiceSpec(
        program=str(engine_web_executable()),
        config=str(config),
        port=configured_port(config),
        log=str(log_path()),
        tools=detect_tools() if tools is None else tools,
    )


def render_launch_agent(spec: ServiceSpec) -> bytes:
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [spec.program],
        "EnvironmentVariables": spec.environment(),
        "WorkingDirectory": str(Path(spec.config).parent),
        "StandardOutPath": spec.log,
        "StandardErrorPath": spec.log,
        "RunAtLoad": True,
        # Restart after a crash, but not after a clean `engine daemon stop`.
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "ExitTimeOut": int(STOP_TIMEOUT_SECONDS),
        "Umask": 0o077,
    })


def _systemd_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def render_systemd_unit(spec: ServiceSpec) -> str:
    environment = "\n".join(
        f"Environment={_systemd_quote(f'{name}={value}')}" for name, value in spec.environment().items()
    )
    return (
        "[Unit]\n"
        "Description=OpenEngine web service\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={_systemd_quote(spec.program)}\n"
        f"WorkingDirectory={_systemd_quote(str(Path(spec.config).parent))}\n"
        f"{environment}\n"
        f"StandardOutput=append:{spec.log}\n"
        f"StandardError=append:{spec.log}\n"
        "UMask=0077\n"
        "Restart=on-failure\n"
        "KillSignal=SIGTERM\n"
        f"TimeoutStopSec={int(STOP_TIMEOUT_SECONDS)}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


class Backend:
    name = ""

    def install(self, spec: ServiceSpec) -> None:
        """Register the service so it starts at login; the process backend cannot."""

    def uninstall(self) -> None:
        """Undo `install`, so nothing starts at the next login."""

    def start(self, spec: ServiceSpec) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def running(self) -> bool:
        raise NotImplementedError


class LaunchdBackend(Backend):
    name = "launchd"

    @staticmethod
    def _domain() -> str:
        return f"gui/{os.getuid()}"

    @classmethod
    def available(cls) -> bool:
        return sys.platform == "darwin" and _run(["launchctl", "print", cls._domain()]).returncode == 0

    def install(self, spec: ServiceSpec) -> None:
        path = launch_agent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(render_launch_agent(spec))

    def start(self, spec: ServiceSpec) -> None:
        if not launch_agent_path().is_file():
            self.install(spec)
        if not self.running():
            result = _run(["launchctl", "bootstrap", self._domain(), str(launch_agent_path())])
            if result.returncode != 0 and not self.running():
                raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.returncode}")
        _run(["launchctl", "kickstart", f"{self._domain()}/{LABEL}"])

    def uninstall(self) -> None:
        self.stop()
        launch_agent_path().unlink(missing_ok=True)

    def stop(self) -> None:
        # bootout returns before teardown finishes. Even after HTTP goes down,
        # print can still find the old registration and kickstart can succeed
        # without starting a replacement. Wait for removal before setup/start.
        # The agent stays on disk, so it starts again at the next login.
        if self.running():
            result = _run(["launchctl", "bootout", f"{self._domain()}/{LABEL}"])
            deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
            while self.running():
                if result.returncode != 0:
                    raise RuntimeError(f"launchctl bootout failed: {result.stderr.strip() or result.returncode}")
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"launchd did not remove {LABEL} within {STOP_TIMEOUT_SECONDS:g} seconds")
                time.sleep(0.2)

    def running(self) -> bool:
        return _run(["launchctl", "print", f"{self._domain()}/{LABEL}"]).returncode == 0


class SystemdBackend(Backend):
    name = "systemd"

    @staticmethod
    def available() -> bool:
        return (sys.platform.startswith("linux") and shutil.which("systemctl") is not None
                and _run(["systemctl", "--user", "show-environment"]).returncode == 0)

    def install(self, spec: ServiceSpec) -> None:
        path = systemd_unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_systemd_unit(spec), encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", SYSTEMD_UNIT])

    def uninstall(self) -> None:
        _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
        systemd_unit_path().unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])

    def start(self, spec: ServiceSpec) -> None:
        if not systemd_unit_path().is_file():
            self.install(spec)
        result = _run(["systemctl", "--user", "start", SYSTEMD_UNIT])
        if result.returncode != 0:
            raise RuntimeError(f"systemctl --user start failed: {result.stderr.strip() or result.returncode}")

    def stop(self) -> None:
        # SIGTERM, then SIGKILL only after TimeoutStopSec.
        _run(["systemctl", "--user", "stop", SYSTEMD_UNIT])

    def running(self) -> bool:
        return _run(["systemctl", "--user", "is-active", "--quiet", SYSTEMD_UNIT]).returncode == 0


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_line(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        pass
    result = _run(["ps", "-o", "command=", "-p", str(pid)])
    return result.stdout if result.returncode == 0 else None


def runs_program(pid: int, program: str) -> bool:
    """Whether `pid` is still `program`, and not a process that reused its number."""
    command = _command_line(pid)
    return command is not None and program in command


class ProcessBackend(Backend):
    """A detached engine-web tracked by a pidfile, where no service manager is usable."""

    name = "process"

    @staticmethod
    def pid() -> int | None:
        """The recorded engine-web, if that process number still belongs to it."""
        try:
            number, program = pidfile_path().read_text(encoding="utf-8").splitlines()[:2]
            pid = int(number)
        except (OSError, ValueError):
            return None
        _reap(pid)
        if process_alive(pid) and runs_program(pid, program):
            return pid
        pidfile_path().unlink(missing_ok=True)
        return None

    def start(self, spec: ServiceSpec) -> None:
        if self.pid() is not None:
            return
        with _owner_only():
            log = open(spec.log, "ab")
        try:
            process = subprocess.Popen(
                [spec.program], env=spec.environment(), cwd=str(Path(spec.config).parent),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log.close()
        pidfile_path().write_text(f"{process.pid}\n{spec.program}\n", encoding="utf-8")

    def stop(self) -> None:
        pid = self.pid()
        if pid is None:
            return
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while process_alive(pid) and time.monotonic() < deadline:
            _reap(pid)
            time.sleep(0.1)
        if process_alive(pid):
            os.kill(pid, signal.SIGKILL)
        pidfile_path().unlink(missing_ok=True)

    def running(self) -> bool:
        return self.pid() is not None


def _reap(pid: int) -> None:
    """Collect our own child so it stops counting as alive once it exits."""
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass


BACKENDS: dict[str, type[Backend]] = {
    LaunchdBackend.name: LaunchdBackend,
    SystemdBackend.name: SystemdBackend,
    ProcessBackend.name: ProcessBackend,
}


def available_backend() -> Backend:
    if LaunchdBackend.available():
        return LaunchdBackend()
    if SystemdBackend.available():
        return SystemdBackend()
    return ProcessBackend()


@dataclass(frozen=True)
class Record:
    """What setup chose and found, so later commands never re-read the shell."""

    backend: str
    spec: ServiceSpec


def read_record() -> Record | None:
    try:
        payload = json.loads(record_path().read_text(encoding="utf-8"))
        return Record(str(payload["backend"]), ServiceSpec(**payload["spec"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def write_record(record: Record) -> None:
    temporary = record_path().with_suffix(".tmp")
    temporary.write_text(json.dumps({"backend": record.backend, "spec": asdict(record.spec)}, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, record_path())


def current() -> tuple[Backend, ServiceSpec]:
    """The recorded service, or a detached process when setup never ran."""
    record = read_record()
    if record is not None and record.backend in BACKENDS:
        return BACKENDS[record.backend](), _with_live_port(record.spec)
    # Startup owns Node validation and diagnostics for an unrecorded service.
    return ProcessBackend(), build_spec(tools=_path_tools())


def _with_live_port(spec: ServiceSpec) -> ServiceSpec:
    """engine-web reads `[server] port` itself at start, so follow edits to it."""
    try:
        return replace(spec, port=configured_port(Path(spec.config)))
    except FileNotFoundError:
        return spec


@contextmanager
def _owner_only() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def prepare_directories() -> None:
    with _owner_only():
        log_directory().mkdir(parents=True, exist_ok=True)
    state_directory().chmod(0o700)


@contextmanager
def instance_lock() -> Iterator[None]:
    """One start or stop at a time for this user."""
    import fcntl

    prepare_directories()
    with open(state_directory() / "daemon.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


API_VERSION = 1


def check_health(url: str, timeout: float = 2.0) -> tuple[str, dict[str, Any] | None, str]:
    """Read the `/api/health` envelope; the one parser `engine` and `engine daemon` share.

    The state is `ready`, `starting`, `incompatible` (an OpenEngine with another
    API version), `foreign` (another program holds the port) or `down`.
    """
    try:
        with urlopen(Request(f"{url}/api/health", headers={"Accept": "application/json"}), timeout=timeout) as response:
            body = json.loads(response.read())
    except HTTPError as error:
        if error.code == 503:
            try:
                body = json.loads(error.read())
            except (OSError, ValueError):
                body = None
            if isinstance(body, dict) and body.get("service") == "openengine":
                return "starting", body, "OpenEngine is starting or not ready"
        return "foreign", None, f"server returned HTTP {error.code}"
    except (URLError, TimeoutError, OSError) as error:
        return "down", None, f"cannot reach {url}: {error.reason if isinstance(error, URLError) else error}"
    except ValueError:
        return "foreign", None, "health endpoint did not return JSON"
    if not isinstance(body, dict) or body.get("service") != "openengine":
        return "foreign", body if isinstance(body, dict) else None, "endpoint is not an OpenEngine service"
    if body.get("api_version") != API_VERSION:
        return "incompatible", body, f"unsupported API compatibility version: {body.get('api_version')!r}"
    if body.get("ready") is not True:
        return "starting", body, "OpenEngine is not ready"
    return "ready", body, f"OpenEngine {body.get('version', 'unknown')} is ready"


def health(url: str, timeout: float = 2.0) -> tuple[str, dict[str, Any] | None]:
    """`ready`, `starting`, `foreign` or `down`; any OpenEngine on the port is ours to manage."""
    state, body, _detail = check_health(url, timeout)
    if state == "incompatible":
        state = "ready" if body and body.get("ready") is True else "starting"
    return state, body


def wait_for(url: str, wanted: set[str], timeout: float, backend: Backend | None = None) -> tuple[str, dict[str, Any] | None]:
    """Poll health until a wanted state, or until a detached process has exited."""
    deadline = time.monotonic() + timeout
    while True:
        state, body = health(url, timeout=1.0)
        if state in wanted or time.monotonic() >= deadline:
            return state, body
        if isinstance(backend, ProcessBackend) and not backend.running():
            return state, body
        time.sleep(0.2)


def start_service() -> tuple[str, dict[str, Any] | None, str]:
    """Start the one per-user instance if it is not already serving."""
    with instance_lock():
        backend, spec = current()
        state, body = health(spec.url)
        if state == "foreign":
            raise RuntimeError(f"another program is using {spec.url}; free port {spec.port} or change [server] port in {spec.config}")
        if state == "down":
            check = _check_node(spec.tools)
            tools, problem = check.tools, check.problem
            if tools != spec.tools:
                spec = replace(spec, tools=tools)
                backend.stop()
                backend.install(spec)
                write_record(Record(backend.name, spec))
                print(f"engine daemon: {_healing_notice(check)}", file=sys.stderr)
            elif problem:
                detail = _healing_notice(check) if check.healed else NODE_HELP
                print(f"engine daemon: {detail}" if check.healed else f"engine daemon: warning: {problem}; {detail}", file=sys.stderr)
            rotate_log(Path(spec.log))
            backend.start(spec)
        state, body = wait_for(spec.url, {"ready", "foreign"}, START_TIMEOUT_SECONDS, backend)
        if state != "ready":
            raise RuntimeError(f"OpenEngine did not become ready at {spec.url}; see {spec.log}")
        return state, body, spec.url


def stop_service() -> str:
    with instance_lock():
        backend, spec = current()
        backend.stop()
        state, _body = wait_for(spec.url, {"down", "foreign"}, STOP_TIMEOUT_SECONDS)
        if state in {"ready", "starting"}:
            raise RuntimeError(f"OpenEngine is still answering at {spec.url}; it may not be managed by engine daemon")
        return spec.url


def open_browser(url: str) -> None:
    if not webbrowser.open(url):
        print(f"Open {url} in a browser.")


def _fail(error: Exception) -> int:
    print(f"engine daemon: {error}", file=sys.stderr)
    return EXIT_FAILED


def command_open(arguments: argparse.Namespace) -> int:
    try:
        _state, _body, url = start_service()
    except (OSError, RuntimeError, ValueError) as error:
        return _fail(error)
    print(f"OpenEngine is running at {url}")
    if not arguments.no_browser:
        open_browser(url)
    return EXIT_OK


def command_setup(arguments: argparse.Namespace) -> int:
    try:
        prepare_directories()
        spec = build_spec()
        with instance_lock():
            previous = read_record()
            if previous is not None and previous.backend in BACKENDS:
                # Rerunning setup after an upgrade moves the service onto it.
                BACKENDS[previous.backend]().stop()
                wait_for(previous.spec.url, {"down", "foreign"}, STOP_TIMEOUT_SECONDS)
            backend = available_backend()
            backend.install(spec)
            state, _body = health(spec.url)
            if state == "foreign":
                raise RuntimeError(f"another program is using {spec.url}; free port {spec.port} or change [server] port in {spec.config}")
            if state != "down":
                print(f"engine daemon: an OpenEngine not started by engine daemon is already at {spec.url}; "
                      "stop it and run engine daemon start to use the service", file=sys.stderr)
            else:
                rotate_log(Path(spec.log))
                try:
                    backend.start(spec)
                except RuntimeError as error:
                    print(f"engine daemon: {error}; running a detached process instead", file=sys.stderr)
                    # Otherwise the OS would start a second, untracked copy at login.
                    backend.uninstall()
                    backend = ProcessBackend()
                    backend.start(spec)
            write_record(Record(backend.name, spec))
    except (OSError, RuntimeError, ValueError) as error:
        return _fail(error)
    print(f"Registered OpenEngine with {backend.name}; logs go to {spec.log}")
    for name in TOOLS:
        print(f"  {name}: {spec.tools.get(name, 'not found')}")
    result = command_open(arguments)
    if result != EXIT_OK:
        # Leave nothing restarting in the background after a failed setup.
        backend.stop()
    return result


def command_start(_arguments: argparse.Namespace) -> int:
    try:
        _state, body, url = start_service()
    except (OSError, RuntimeError, ValueError) as error:
        return _fail(error)
    print(f"OpenEngine {(body or {}).get('version', 'unknown')} is running at {url}")
    return EXIT_OK


def command_stop(_arguments: argparse.Namespace) -> int:
    try:
        url = stop_service()
    except (OSError, RuntimeError, ValueError) as error:
        return _fail(error)
    print(f"OpenEngine at {url} is stopped")
    return EXIT_OK


def command_status(arguments: argparse.Namespace) -> int:
    try:
        backend, spec = current()
    except (OSError, RuntimeError, ValueError) as error:
        return _fail(error)
    state, body = health(spec.url)
    report = {
        "url": spec.url,
        "health": state,
        "version": (body or {}).get("version"),
        "engine": version("engine-cli"),
        "backend": backend.name,
        "running": backend.running(),
        "config": spec.config,
        "log": spec.log,
    }
    if arguments.json:
        print(json.dumps(report, sort_keys=True))
    else:
        for name in ("url", "health", "version", "engine", "backend", "running", "config", "log"):
            print(f"{name}: {report[name] if report[name] is not None else 'unknown'}")
    return EXIT_OK if state == "ready" else EXIT_FAILED


def tail(file: BinaryIO, count: int, block: int = 64 * 1024) -> list[bytes]:
    """The last `count` lines, read backwards from the end so cost follows `count`."""
    end = file.seek(0, os.SEEK_END)
    position, data = end, b""
    while count > 0 and position > 0 and data.count(b"\n") <= count:
        step = min(block, position)
        position -= step
        file.seek(position)
        data = file.read(step) + data
    file.seek(end)
    return data.splitlines(keepends=True)[-count:] if count > 0 else []


def command_logs(arguments: argparse.Namespace) -> int:
    path = log_path()
    if not path.is_file():
        print(f"engine daemon: no log at {path} yet", file=sys.stderr)
        return EXIT_FAILED
    with path.open("rb") as file:
        for line in tail(file, arguments.lines):
            print(line.decode("utf-8", errors="replace"), end="")
        if not arguments.follow:
            return EXIT_OK
        try:
            while True:
                line = file.readline()
                if line:
                    print(line.decode("utf-8", errors="replace"), end="", flush=True)
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return EXIT_OK


@dataclass(frozen=True)
class Finding:
    name: str
    level: str  # "ok", "warn" or "error"
    detail: str


def _port_finding(port: int) -> Finding:
    url = service_url(port)
    state, body = health(url)
    if state in {"ready", "starting"}:
        return Finding("port", "ok", f"{url} is served by OpenEngine {(body or {}).get('version', '')}".rstrip())
    if state == "foreign":
        return Finding("port", "error", f"{url} is served by another program")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((HOST, port))
        except OSError as error:
            return Finding("port", "error", f"cannot bind {HOST}:{port}: {error}")
    return Finding("port", "ok", f"{HOST}:{port} is free")


def _claude_login_finding(spec: ServiceSpec) -> Finding:
    environment = spec.environment()
    try:
        loaded = load_engine_config(spec.config)
        if (directory := loaded.claude_config_dir) is not None:
            environment["CLAUDE_CONFIG_DIR"] = str(directory)
    except ValueError as error:
        return Finding("claude login", "error", str(error))
    directory = environment.get("CLAUDE_CONFIG_DIR", str(Path(environment["HOME"]) / ".claude"))
    fix = f"run claude /login with CLAUDE_CONFIG_DIR={shlex.quote(directory)}"
    executable = spec.tools.get("claude") or shutil.which("claude", path=environment["PATH"])
    if executable is None:
        return Finding("claude login", "warn", f"claude not on service PATH; rerun engine daemon setup; {fix}")
    try:
        result = subprocess.run(
            [executable, "auth", "status"], env=environment,
            capture_output=True, text=True, check=False, timeout=15,
        )
        status = json.loads(result.stdout)
        if result.returncode == 0 and isinstance(status, dict) and status.get("loggedIn") is True:
            return Finding("claude login", "ok", f"{status.get('email') or 'logged in (email unavailable)'} (CLAUDE_CONFIG_DIR={directory})")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return Finding("claude login", "warn", fix)


def diagnose() -> list[Finding]:
    config = config_path()
    findings: list[Finding] = []
    try:
        port = configured_port(config)
    except FileNotFoundError:
        findings.append(Finding("config", "error", f"{config} does not exist; run the installer or set ENGINE_CONFIG"))
        port = None
    except (OSError, ValueError) as error:
        findings.append(Finding("config", "error", f"{config}: {error}"))
        port = None
    else:
        findings.append(Finding("config", "ok", str(config)))
    if port is not None:
        findings.append(_port_finding(port))
    try:
        findings.append(Finding("engine-web", "ok", str(engine_web_executable())))
    except RuntimeError as error:
        findings.append(Finding("engine-web", "error", str(error)))
    record = read_record()
    recorded = record.spec.tools if record else {}
    found = _path_tools()
    checked = recorded if record else found
    check = _check_node(checked)
    healed, problem = check.tools, check.problem
    for name in TOOLS:
        path = recorded.get(name) or found.get(name)
        if name == "node" and problem:
            if check.healed:
                findings.append(Finding(name, "warn", f"{problem}; engine daemon start will use {healed['node']}; {_healing_notice(check, verbose=True)}"))
            else:
                findings.append(Finding(name, "error", f"{problem}; {NODE_HELP}"))
        elif path:
            note = "" if record is None or name in recorded else " (on PATH, but not recorded; rerun engine daemon setup)"
            findings.append(Finding(name, "ok", path + note))
        elif name in REQUIRED_TOOLS:
            findings.append(Finding(name, "warn", f"{name} not found; install it and rerun engine daemon setup"))
        else:
            findings.append(Finding(name, "warn", f"{name} not found; optional, needed only to run that agent"))
    if port is not None and ("claude" in recorded or "claude" in found):
        spec = record.spec if record else ServiceSpec(
            program="", config=str(config), port=port, log="", tools=found,
        )
        findings.append(_claude_login_finding(spec))
    return findings


def command_doctor(arguments: argparse.Namespace) -> int:
    findings = diagnose()
    if arguments.json:
        print(json.dumps({"checks": [asdict(finding) for finding in findings]}, sort_keys=True))
    else:
        for finding in findings:
            print(f"{finding.level:<5}  {finding.name}: {finding.detail}")
    return EXIT_FAILED if any(finding.level == "error" for finding in findings) else EXIT_OK


def add_parser(commands: argparse._SubParsersAction) -> None:
    daemon = commands.add_parser("daemon", help="run the local OpenEngine service in the background")
    daemon.add_argument("--no-browser", action="store_true", help="start without opening a browser")
    actions = daemon.add_subparsers(dest="daemon_command")
    for name, help_text in (
        ("open", "start the service if needed and open it in a browser"),
        ("setup", "register the service to run at login, then start it"),
    ):
        action = actions.add_parser(name, help=help_text)
        # SUPPRESS keeps `engine daemon --no-browser open` from being reset here.
        action.add_argument("--no-browser", action="store_true", default=argparse.SUPPRESS, help="do not open a browser")
    actions.add_parser("start", help="start the service if it is not running")
    actions.add_parser("stop", help="stop the service gracefully")
    status = actions.add_parser("status", help="show version, health and URL")
    status.add_argument("--json", action="store_true")
    logs = actions.add_parser("logs", help="print the service log")
    logs.add_argument("-n", "--lines", type=int, default=100, help="lines to print (default: 100)")
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    doctor = actions.add_parser("doctor", help="check config, port and tools")
    doctor.add_argument("--json", action="store_true")


def main(arguments: argparse.Namespace) -> int:
    handlers = {
        None: command_open,
        "open": command_open,
        "setup": command_setup,
        "start": command_start,
        "stop": command_stop,
        "status": command_status,
        "logs": command_logs,
        "doctor": command_doctor,
    }
    return handlers[arguments.daemon_command](arguments)

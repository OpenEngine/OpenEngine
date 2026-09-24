"""Diagnose and inspect an OpenEngine service from a terminal.

This is deliberately the first, read-only CLI slice.  It can establish that a
server is the compatible OpenEngine service, but it never starts one; local
service lifecycle belongs to the next delivery.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, urlunsplit

from platformdirs import user_config_path, user_data_path, user_state_path

DEFAULT_SERVER = "http://127.0.0.1:4364"
CONFIG_ENVIRONMENT_VARIABLE = "ENGINE_CLI_CONFIG"
STATE_ENVIRONMENT_VARIABLE = "ENGINE_CLI_STATE_DIR"
STARTUP_TIMEOUT_SECONDS = 30.0
STARTUP_LOCK_TIMEOUT_SECONDS = 35.0
EXIT_OK = 0
EXIT_UNHEALTHY = 1
EXIT_USAGE = 2


@dataclass(frozen=True)
class Profile:
    server: str = DEFAULT_SERVER
    last_repository: str = ""
    last_task: str = ""


@dataclass(frozen=True)
class Preferences:
    selected_profile: str = "default"
    profiles: dict[str, Profile] | None = None

    def profile(self) -> Profile:
        return (self.profiles or {}).get(self.selected_profile, Profile())


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class ServiceRecord:
    pid: int
    server: str
    log: str


def preferences_path() -> Path:
    override = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE)
    return Path(override) if override else user_config_path("openengine") / "cli.json"


def state_path() -> Path:
    override = os.environ.get(STATE_ENVIRONMENT_VARIABLE)
    return Path(override) if override else user_state_path("openengine") / "cli"


def record_path() -> Path:
    return state_path() / "service.json"


def log_path() -> Path:
    return state_path() / "service.log"


def load_preferences(path: Path | None = None) -> Preferences:
    path = path or preferences_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Preferences()
    except (OSError, json.JSONDecodeError):
        return Preferences()
    profiles = {
        name: Profile(
            server=value.get("server", DEFAULT_SERVER),
            last_repository=value.get("lastRepository", ""),
            last_task=value.get("lastTask", ""),
        )
        for name, value in payload.get("profiles", {}).items()
        if isinstance(name, str) and isinstance(value, dict) and isinstance(value.get("server", DEFAULT_SERVER), str)
    }
    selected = payload.get("selectedProfile", "default")
    return Preferences(selected if isinstance(selected, str) and selected else "default", profiles)


def save_preferences(preferences: Preferences, path: Path | None = None) -> None:
    path = path or preferences_path()
    payload = {
        "selectedProfile": preferences.selected_profile,
        "profiles": {
            name: {
                "server": profile.server,
                "lastRepository": profile.last_repository,
                "lastTask": profile.last_task,
            }
            for name, profile in (preferences.profiles or {}).items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def normalize_server(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.netloc
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("server must be an HTTP(S) origin, for example http://127.0.0.1:4364")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def probe(server: str, timeout: float = 3.0) -> tuple[Check, dict[str, Any] | None]:
    try:
        with urlopen(Request(f"{server}/api/health", headers={"Accept": "application/json"}), timeout=timeout) as response:
            body = json.loads(response.read())
    except HTTPError as error:
        if error.code == 503:
            try:
                body = json.loads(error.read())
            except (json.JSONDecodeError, OSError):
                body = None
            if isinstance(body, dict) and body.get("service") == "openengine":
                return Check("service", False, "OpenEngine is starting or not ready"), body
        return Check("service", False, f"server returned HTTP {error.code}"), None
    except (URLError, TimeoutError, OSError) as error:
        return Check("service", False, f"cannot reach {server}: {error.reason if isinstance(error, URLError) else error}"), None
    except json.JSONDecodeError:
        return Check("service", False, "health endpoint did not return JSON"), None
    if not isinstance(body, dict) or body.get("service") != "openengine":
        return Check("service", False, "endpoint is not an OpenEngine service"), body if isinstance(body, dict) else None
    if body.get("api_version") != 1:
        return Check("service", False, f"unsupported API compatibility version: {body.get('api_version')!r}"), body
    if body.get("ready") is not True:
        return Check("service", False, "OpenEngine is not ready"), body
    return Check("service", True, f"OpenEngine {body.get('version', 'unknown')} is ready"), body


def is_openengine(identity: dict[str, Any] | None) -> bool:
    return bool(identity and identity.get("service") == "openengine")


def read_service_record() -> ServiceRecord | None:
    try:
        payload = json.loads(record_path().read_text(encoding="utf-8"))
        return ServiceRecord(int(payload["pid"]), str(payload["server"]), str(payload["log"]))
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


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


def discard_stale_record() -> None:
    record = read_service_record()
    if record is not None and not process_alive(record.pid):
        try:
            record_path().unlink()
        except FileNotFoundError:
            pass


@contextmanager
def startup_lock():
    """A cross-process lock with stale-owner recovery for local service startup."""
    directory = state_path()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "startup.lock"
    deadline = time.monotonic() + STARTUP_LOCK_TIMEOUT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            os.write(descriptor, str(os.getpid()).encode())
        except FileExistsError:
            try:
                owner = int(path.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                owner = 0
            if owner and not process_alive(owner):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("another engine command is still starting the local service")
            time.sleep(0.1)
    try:
        yield
    finally:
        os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def launch_local_service(server: str) -> subprocess.Popen[bytes]:
    executable = shutil.which("engine-web")
    if executable is None:
        raise RuntimeError("engine-web is not installed; install the OpenEngine web service before starting it")
    directory = state_path()
    directory.mkdir(parents=True, exist_ok=True)
    log = log_path().open("ab")
    options: dict[str, Any] = {"stdout": log, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen([executable], **options)
    finally:
        log.close()
    record_path().write_text(json.dumps({"pid": process.pid, "server": server, "log": str(log_path())}) + "\n", encoding="utf-8")
    return process


def log_hint() -> str:
    return f" See {log_path()} for service output."


def wait_until_ready(server: str, process: subprocess.Popen[bytes] | None = None) -> tuple[Check, dict[str, Any] | None]:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    last: tuple[Check, dict[str, Any] | None] = (Check("service", False, "OpenEngine is starting"), None)
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            return Check("service", False, f"engine-web exited with {process.returncode}.{log_hint()}"), None
        last = probe(server, timeout=1.0)
        if last[0].ok:
            return last
        # A different healthy HTTP service must never be mistaken for ours.
        if last[1] is not None and not is_openengine(last[1]):
            return last
        time.sleep(0.1)
    return Check("service", False, f"OpenEngine did not become ready within {STARTUP_TIMEOUT_SECONDS:g} seconds.{log_hint()}"), last[1]


def ensure_service(server: str) -> tuple[Check, dict[str, Any] | None, bool]:
    """Return a compatible service, starting only the agreed default local one."""
    check, identity = probe(server)
    if check.ok or server != DEFAULT_SERVER:
        return check, identity, False
    try:
        with startup_lock():
            discard_stale_record()
            check, identity = probe(server)
            if check.ok:
                return check, identity, False
            if is_openengine(identity):
                ready, identity = wait_until_ready(server)
                return ready, identity, False
            if identity is not None:
                return check, identity, False
            process = launch_local_service(server)
            ready, identity = wait_until_ready(server, process)
            return ready, identity, True
    except (OSError, RuntimeError, TimeoutError) as error:
        return Check("service", False, f"could not start local OpenEngine service: {error}"), None, False


def executable_check(name: str) -> Check:
    path = shutil.which(name)
    return Check(name, bool(path), path or f"{name} is not on PATH")


def writable_check(name: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".engine-write-check-", delete=True):
            pass
    except OSError as error:
        return Check(name, False, f"{path}: {error}")
    return Check(name, True, str(path))


def source_control_check(server: str, service_ok: bool) -> Check:
    if not service_ok:
        return Check("source_control", False, "not checked because the service is unavailable")
    try:
        with urlopen(f"{server}/api/source-control/status", timeout=3.0) as response:
            status = json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return Check("source_control", False, f"could not read selected provider: {error}")
    provider = status.get("provider") if isinstance(status, dict) else None
    return Check("source_control", bool(provider), str(provider or "no provider selected"))


def selected_server(arguments: argparse.Namespace, preferences: Preferences) -> str:
    candidate = arguments.server if getattr(arguments, "server", None) else preferences.profile().server
    return normalize_server(candidate)


def render(checks: list[Check], as_json: bool, extra: dict[str, Any] | None = None) -> None:
    if as_json:
        result: dict[str, Any] = {"checks": [asdict(check) for check in checks]}
        if extra:
            result.update(extra)
        print(json.dumps(result, sort_keys=True))
        return
    for check in checks:
        print(f"{'ok' if check.ok else 'error'}  {check.name}: {check.detail}")


def status(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server = selected_server(arguments, preferences)
    except ValueError as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_USAGE
    check, identity, started = ensure_service(server)
    render([check], arguments.json, {"server": server, "identity": identity, "started": started})
    return EXIT_OK if check.ok else EXIT_UNHEALTHY


def doctor(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server = selected_server(arguments, preferences)
    except ValueError as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_USAGE
    service, identity = probe(server)
    checks = [
        service,
        executable_check("git"),
        executable_check("codex"),
        executable_check("claude"),
        source_control_check(server, service.ok),
        writable_check("config", preferences_path().parent),
        writable_check("data", user_data_path("openengine")),
    ]
    render(checks, arguments.json, {"server": server, "identity": identity})
    return EXIT_OK if all(check.ok for check in checks) else EXIT_UNHEALTHY


def configure_server(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server = normalize_server(arguments.url)
    except ValueError as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_USAGE
    profiles = dict(preferences.profiles or {})
    current = profiles.get(preferences.selected_profile, Profile())
    profiles[preferences.selected_profile] = Profile(server, current.last_repository, current.last_task)
    save_preferences(Preferences(preferences.selected_profile, profiles))
    if arguments.json:
        print(json.dumps({"profile": preferences.selected_profile, "server": server}, sort_keys=True))
    else:
        print(f"server for profile {preferences.selected_profile!r}: {server}")
    return EXIT_OK


def configure_profile(arguments: argparse.Namespace, preferences: Preferences) -> int:
    name = arguments.name.strip()
    if not name:
        print("engine: profile name cannot be empty", file=sys.stderr)
        return EXIT_USAGE
    profiles = dict(preferences.profiles or {})
    profiles.setdefault(name, Profile())
    save_preferences(Preferences(name, profiles))
    if arguments.json:
        print(json.dumps({"profile": name, "server": profiles[name].server}, sort_keys=True))
    else:
        print(f"selected profile: {name}")
    return EXIT_OK


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="engine", description=__doc__)
    result.add_argument("--version", action="version", version=version("engine-cli"))
    commands = result.add_subparsers(dest="command")
    for name, help_text in (("status", "show OpenEngine service identity and readiness"), ("doctor", "diagnose local prerequisites and service access")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--server", metavar="URL", help="override the configured OpenEngine service")
        command.add_argument("--json", action="store_true", help="emit a stable machine-readable report")
    config = commands.add_parser("config", help="manage persistent CLI preferences")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    server = config_commands.add_parser("server", help="set the selected profile's service URL")
    server.add_argument("url")
    server.add_argument("--json", action="store_true")
    profile = config_commands.add_parser("profile", help="select or create a named profile")
    profile.add_argument("name")
    profile.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command is None:
        parser().print_help()
        return EXIT_USAGE
    preferences = load_preferences()
    if arguments.command == "status":
        return status(arguments, preferences)
    if arguments.command == "doctor":
        return doctor(arguments, preferences)
    if arguments.command == "config" and arguments.config_command == "server":
        return configure_server(arguments, preferences)
    if arguments.command == "config" and arguments.config_command == "profile":
        return configure_profile(arguments, preferences)
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())

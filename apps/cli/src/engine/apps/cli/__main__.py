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
import textwrap
import threading
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from platformdirs import user_config_path, user_data_path, user_state_path

from engine.apps.cli import daemon, onboarding
from engine.domain import (
    STATE_INPUT, TRIAGE_TOOL, WorkState, finding_comment, review_inputs,
)
from engine.runtime.change_requests import ChangeRequest, change_request, remote_project

DEFAULT_SERVER = "http://127.0.0.1:4364"
CONFIG_ENVIRONMENT_VARIABLE = "ENGINE_CLI_CONFIG"
# `ENGINE_CONFIG` was used by early local-development commands. Keep it as a
# fallback so those commands isolate CLI preferences as intended, while the
# more specific name remains authoritative when both are present.
LEGACY_CONFIG_ENVIRONMENT_VARIABLE = "ENGINE_CONFIG"
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
    override = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE) or os.environ.get(
        LEGACY_CONFIG_ENVIRONMENT_VARIABLE
    )
    return Path(override) if override else user_config_path("openengine") / "cli.json"


def service_token() -> str:
    """Reuse the server's existing local bearer credential without a new login."""
    if token := os.environ.get("ENGINE_SERVICE_TOKEN"):
        return token
    try:
        for line in (Path.cwd() / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("ENGINE_SERVICE_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def request_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    if token := service_token():
        headers["Authorization"] = f"Bearer {token}"
    return headers


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
    state, body, detail = daemon.check_health(server, timeout)
    return Check("service", state == "ready", detail), body


def is_openengine(identity: dict[str, Any] | None) -> bool:
    return bool(identity and identity.get("service") == "openengine")


def read_service_record() -> ServiceRecord | None:
    try:
        payload = json.loads(record_path().read_text(encoding="utf-8"))
        return ServiceRecord(int(payload["pid"]), str(payload["server"]), str(payload["log"]))
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


process_alive = daemon.process_alive


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
    if daemon.read_record() is not None:
        # `engine daemon setup` registered the service, so start that one rather
        # than a second, untracked engine-web.
        try:
            _state, _body, url = daemon.start_service()
        except (OSError, RuntimeError, ValueError) as error:
            return Check("service", False, f"could not start the OpenEngine service: {error}"), None, False
        check, identity = probe(url)
        return check, identity, True
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


def fetch_json(server: str, path: str) -> dict[str, Any]:
    """Read a small service resource, preserving a useful command-line error."""
    try:
        with urlopen(Request(f"{server}{path}", headers=request_headers({"Accept": "application/json"})), timeout=5.0) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        if error.code == 404:
            raise RuntimeError("not found") from None
        if error.code == 401:
            raise RuntimeError("service requires browser login; sign in through /web first") from None
        raise RuntimeError(f"server returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read {path}: {error}") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} did not return a JSON object")
    return payload


def request_json(server: str, path: str, body: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    request = Request(
        f"{server}{path}", data=json.dumps(body).encode(), method="POST",
        headers=request_headers({"Accept": "application/json", "Content-Type": "application/json"}),
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error")
        except (OSError, json.JSONDecodeError, AttributeError):
            detail = None
        raise RuntimeError(str(detail or f"server returned HTTP {error.code}")) from None
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not post {path}: {error}") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} did not return a JSON object")
    return payload


def post_empty(server: str, path: str, body: dict[str, Any]) -> None:
    request = Request(f"{server}{path}", data=json.dumps(body).encode(), method="POST", headers=request_headers({"Content-Type": "application/json"}))
    try:
        with urlopen(request, timeout=10.0):
            return
    except HTTPError as error:
        raise RuntimeError(f"server returned HTTP {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise RuntimeError(f"could not post {path}: {error}") from None


def content_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    return "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))


class TerminalSpinner:
    """Show that a streaming request is alive before its first event arrives."""

    def __init__(self, message: str = "OpenEngine is thinking") -> None:
        self.message = message
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._finished = False

    def start(self) -> None:
        if not sys.stderr.isatty():
            return
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._stopped.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
            print("\r\x1b[K", end="", file=sys.stderr, flush=True)

    def _spin(self) -> None:
        while not self._stopped.is_set():
            for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏":
                print(f"\r{frame} {self.message}…", end="", file=sys.stderr, flush=True)
                if self._stopped.wait(0.1):
                    return


def stream_run(server: str, path: str, body: dict[str, Any] | None = None) -> int:
    request = Request(
        f"{server}{path}", data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers=request_headers({"Accept": "application/x-ndjson", **({"Content-Type": "application/json"} if body is not None else {})}),
    )
    spinner = TerminalSpinner()
    spinner.start()
    last_content = ""
    reply_started = False
    try:
        with urlopen(request, timeout=30.0) as response:
            if response.status == 204:
                spinner.stop()
                print("No active run.")
                return EXIT_OK
            for line in response:
                if not line.strip():
                    continue
                spinner.stop()
                event = json.loads(line)
                if event.get("type") == "content":
                    content = content_text(event.get("content"))
                    if not reply_started:
                        print("• OpenEngine")
                        print("  ", end="", flush=True)
                        reply_started = True
                    delta = content.removeprefix(last_content)
                    print(delta, end="", flush=True)
                    last_content = content
                elif event.get("type") == "approval" and isinstance(event.get("approval"), dict):
                    approval = event["approval"]
                    print("\nApproval required:")
                    print(json.dumps(approval, indent=2, sort_keys=True))
                    if sys.stdin.isatty():
                        action = palette(["Approve", "Reject", "View details", "Defer"], "Decision: ")
                        if action == "View details":
                            print(json.dumps(approval, indent=2, sort_keys=True))
                            action = palette(["Approve", "Reject", "Defer"], "Decision: ")
                        if action in {"Approve", "Reject"}:
                            thread_id = path.split("/")[3]
                            try:
                                request_json(
                                    server,
                                    f"/api/threads/{thread_id}/runs/current/approvals/{approval['id']}",
                                    {"decision": "accept" if action == "Approve" else "cancel"},
                                )
                            except RuntimeError as error:
                                print(f"Approval was not applied: {error}. Continuing stream.")
                            else:
                                print("Approval sent; continuing stream.")
                    else:
                        print("Review it with /approvals in the interactive workbench or in the web UI.")
                elif event.get("type") == "error":
                    if reply_started:
                        print()
                    print(f"\nengine: {event.get('error')}", file=sys.stderr)
                    return EXIT_UNHEALTHY
                elif event.get("type") == "done":
                    text = content_text(event.get("content"))
                    if text and text != last_content:
                        if not reply_started:
                            print("• OpenEngine")
                        print(f"\n  {text}")
                    elif reply_started:
                        print()
                    return EXIT_OK
    except KeyboardInterrupt:
        print("\nDetached; the service-side run continues.")
        return EXIT_OK
    except HTTPError as error:
        print(f"engine: server returned HTTP {error.code}", file=sys.stderr)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        print(f"engine: stream disconnected: {error}", file=sys.stderr)
    finally:
        spinner.stop()
    return EXIT_UNHEALTHY


def selected_server(arguments: argparse.Namespace, preferences: Preferences) -> str:
    candidate = arguments.server if getattr(arguments, "server", None) else preferences.profile().server
    return normalize_server(candidate)


def read_service(arguments: argparse.Namespace, preferences: Preferences) -> tuple[str, Check]:
    server = selected_server(arguments, preferences)
    check, _identity, _started = ensure_service(server)
    return server, check


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


def _connection_state(connected: object, configured: object = True) -> str:
    if connected is True:
        return "connected"
    if configured is True:
        return "not connected"
    return "not configured"


CONNECTION_PATHS = {
    "github": "/api/github/status",
    "sourceControl": "/api/source-control/status",
    "gitlab": "/api/gitlab/status",
    "slack": "/api/slack/status",
}
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def connection_lines(
    snapshot: dict[str, dict[str, Any]], *, loading: set[str] | None = None,
    errors: dict[str, str] | None = None, spinner: str = "⠋",
) -> list[str]:
    """Render Settings status, including partial results while requests run."""
    loading = loading or set()
    errors = errors or {}
    github = snapshot.get("github", {})
    source_control = snapshot.get("sourceControl", {})
    gitlab = snapshot.get("gitlab", {})
    slack = snapshot.get("slack", {})
    gh_cli = source_control.get("ghCli")
    gh_cli = gh_cli if isinstance(gh_cli, dict) else {}
    provider = source_control.get("provider") if source_control else None
    account = gh_cli.get("account")
    source_connected = False
    if provider == "gh-cli" and gh_cli.get("authenticated") is True:
        provider_detail = f"GitHub CLI{f' as {account}' if account else ''}"
        source_connected = True
    elif provider == "github-oauth":
        provider_detail = "GitHub OAuth"
        source_connected = github.get("connected") is True
    elif provider == "gitlab-oauth":
        provider_detail = "GitLab OAuth"
        source_connected = gitlab.get("connected") is True
    else:
        provider_detail = "none selected"
    slack_state = _connection_state(slack.get("connected"), slack.get("configured"))
    if slack.get("connected") is True:
        slack_state += "; events ready" if slack.get("events") is True else "; events not ready"
    gitlab_origin = gitlab.get("origin") or "https://gitlab.com"

    lines = ["Settings", "Source control"]
    if "sourceControl" in loading:
        lines.append(f"{spinner}  Active provider: checking…")
    elif "sourceControl" in errors:
        lines.append("!  Active provider: unavailable")
    else:
        active_state = "connected" if source_connected else "not connected"
        lines.append(f"{'✓' if source_connected else '○'}  Active provider: {provider_detail} ({active_state})")

    def integration_line(name: str, label: str, connected: object, configured: object) -> str:
        if name in loading:
            return f"{spinner}  {label}: checking…"
        if name in errors:
            return f"!  {label}: unavailable"
        state = _connection_state(connected, configured)
        suffix = " — available, not active" if connected is True else ""
        return f"{'✓' if connected is True else '○'}  {label}: {state}{suffix}"

    if provider != "github-oauth":
        lines.append(integration_line("github", "GitHub OAuth", github.get("connected"), github.get("clientIdConfigured")))
    if provider != "gitlab-oauth":
        lines.append(integration_line("gitlab", f"GitLab ({gitlab_origin})", gitlab.get("connected"), gitlab.get("clientIdConfigured")))
    lines.append("Messaging")
    if "slack" in loading:
        lines.append(f"{spinner}  Slack: checking…")
    elif "slack" in errors:
        lines.append("!  Slack: unavailable")
    else:
        lines.append(f"{'✓' if slack.get('connected') is True else '○'}  Slack: {slack_state}")
    return lines


def progressive_connection_snapshot(server: str) -> dict[str, dict[str, Any]]:
    """Fetch connection statuses concurrently and update the Settings panel in place."""
    snapshot: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    lock = threading.Lock()

    def load(name: str, path: str) -> None:
        try:
            result = fetch_json(server, path)
            with lock:
                snapshot[name] = result
        except RuntimeError as error:
            with lock:
                errors[name] = str(error)

    workers = [threading.Thread(target=load, args=(name, path), daemon=True) for name, path in CONNECTION_PATHS.items()]
    for worker in workers:
        worker.start()
    rendered_rows = 0
    frame = 0
    while any(worker.is_alive() for worker in workers):
        with lock:
            loading = set(CONNECTION_PATHS) - set(snapshot) - set(errors)
            lines = connection_lines(dict(snapshot), loading=loading, errors=dict(errors), spinner=SPINNER_FRAMES[frame % len(SPINNER_FRAMES)])
        rendered_rows = draw_palette(lines, rendered_rows)
        frame += 1
        time.sleep(0.1)
    for worker in workers:
        worker.join()
    with lock:
        draw_palette(connection_lines(snapshot, errors=errors), rendered_rows)
        return snapshot


def filtered_threads(threads: list[dict[str, Any]], filter_name: str) -> list[dict[str, Any]]:
    if filter_name == "active":
        return [thread for thread in threads if not thread.get("archived")]
    if filter_name == "archived":
        return [thread for thread in threads if thread.get("archived")]
    return threads


def load_threads(server: str, filter_name: str) -> list[dict[str, Any]]:
    payload = fetch_json(server, "/api/threads")
    threads = payload.get("threads")
    if not isinstance(threads, list) or any(not isinstance(thread, dict) for thread in threads):
        raise RuntimeError("service returned invalid thread data")
    return filtered_threads(threads, filter_name)


def render_threads(
    threads: list[dict[str, Any]], as_json: bool, summaries: dict[str, str] | None = None,
) -> None:
    if as_json:
        print(json.dumps({"threads": threads}, sort_keys=True))
        return
    if not threads:
        print("No threads.")
        return
    for thread in threads:
        archived = " archived" if thread.get("archived") else ""
        repository = thread.get("workspaceRoot") or "no repository attached"
        thread_id = str(thread.get("id", ""))
        title = (summaries or {}).get(thread_id) or str(thread.get("title") or "Untitled work order")
        print(f"{title} [{repository}]{archived}")


def thread_summary(server: str, thread: dict[str, Any], *, limit: int = 96) -> str:
    """Prefer a useful opening-request excerpt over a placeholder thread title."""
    title = str(thread.get("title") or "Untitled work order")
    if title not in {"New chat", "New project"}:
        return title
    thread_id = str(thread.get("id") or "")
    if not thread_id:
        return title
    try:
        messages = load_transcript(server, thread_id)
    except RuntimeError:
        return title
    opening = next(
        (content_text(message.get("content")) for message in messages if message.get("role") == "user"),
        "",
    )
    opening = " ".join(opening.split())
    if not opening:
        return title
    return opening if len(opening) <= limit else f"{opening[:limit - 1]}…"


def thread_choice_labels(threads: list[dict[str, Any]], summaries: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Create readable, unique picker labels without exposing thread identifiers."""
    labels: dict[str, dict[str, Any]] = {}
    for thread in threads:
        thread_id = str(thread.get("id", ""))
        base = summaries.get(thread_id) or str(thread.get("title") or "Untitled work order")
        label = base
        suffix = 2
        while label in labels:
            label = f"{base} ({suffix})"
            suffix += 1
        labels[label] = thread
    return labels


def render_startup_dashboard(server: str) -> None:
    """Show actionable work before offering the interactive prompt."""
    try:
        active = load_threads(server, "active")
    except RuntimeError as error:
        print(f"Work orders: unavailable ({error})")
        return

    summaries = {str(thread.get("id", "")): thread_summary(server, thread) for thread in active[:5]}

    def summary(thread: dict[str, Any]) -> str:
        return summaries.get(str(thread.get("id", ""))) or str(thread.get("title") or "Untitled work order")

    waiting = [thread for thread in active if thread.get("pendingApproval")]
    if waiting:
        print("Action required")
        for thread in waiting:
            print(f"!  {summary(thread)} — approval required")
        print("Use /approvals to review pending decisions.")
    else:
        print("No action required.")

    print(f"Active work orders ({len(active)})")
    if not active:
        print("No active work orders.")
        return
    for thread in active[:5]:
        state = "waiting for approval" if thread.get("pendingApproval") else thread.get("phase", "active")
        print(f"•  {summary(thread)} — {state}")
    if len(active) > 5:
        print(f"… and {len(active) - 5} more. Use /threads to browse all work orders.")


def load_transcript(server: str, thread_id: str) -> list[dict[str, Any]]:
    payload = fetch_json(server, f"/api/threads/{thread_id}/messages")
    messages = payload.get("messages")
    if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
        raise RuntimeError("service returned invalid conversation messages")
    return messages


def visible_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the terminal transcript conversational; tools remain UI-only."""
    visible = []
    for message in messages:
        text = content_text(message.get("content"))
        if not text:
            continue
        visible.append({
            "id": message.get("id"),
            "role": message.get("role"),
            "content": [{"type": "text", "text": text}],
        })
    return visible


def render_chat_message(role: object, text: str) -> None:
    """Render a compact, Codex-like terminal conversation message."""
    width = shutil.get_terminal_size(fallback=(100, 24)).columns
    # Match Codex's terminal-first layout: use the available width, reserving
    # only the marker and indentation rather than an arbitrary reading column.
    message_width = max(24, width - 4)
    lines = [line for paragraph in text.splitlines() or [""] for line in (textwrap.wrap(paragraph, message_width) or [""])]
    is_user = role == "user"
    if is_user:
        print(f"› {lines[0]}")
        for line in lines[1:]:
            print(f"  {line}")
    else:
        print("• OpenEngine")
        for line in lines:
            print(f"  {line}")
    print()


def render_transcript(messages: list[dict[str, Any]], as_json: bool) -> None:
    visible = visible_transcript(messages)
    if as_json:
        print(json.dumps({"messages": visible}, sort_keys=True))
        return
    if not visible:
        print("No conversation messages.")
        return
    for message in visible:
        text = content_text(message.get("content"))
        render_chat_message(message.get("role"), text)


def open_thread(server: str, thread: dict[str, Any], preferences: Preferences) -> None:
    """Open a work order directly into its transcript and continuation prompt."""
    thread_id = str(thread.get("id", ""))
    if not thread_id:
        print("engine: selected work order has no id", file=sys.stderr)
        return
    try:
        remember_thread(preferences, thread)
        render_transcript(load_transcript(server, thread_id), False)
    except RuntimeError as error:
        print(f"engine: {error}", file=sys.stderr)
        return
    reply = prompt_line("Continue this work order: ")
    if reply and reply.strip():
        stream_run(server, f"/api/threads/{thread_id}/runs", {"text": reply.strip()})


def remember_thread(preferences: Preferences, thread: dict[str, Any]) -> None:
    profiles = dict(preferences.profiles or {})
    current = profiles.get(preferences.selected_profile, Profile())
    profiles[preferences.selected_profile] = Profile(
        current.server,
        str(thread.get("workspaceRoot") or current.last_repository),
        str(thread.get("id") or current.last_task),
    )
    save_preferences(Preferences(preferences.selected_profile, profiles))


def creation_defaults(server: str, preferences: Preferences, arguments: argparse.Namespace) -> tuple[str, str, str]:
    config = fetch_json(server, "/api/config")
    agent = getattr(arguments, "agent", None) or config.get("defaultAgent")
    runner = getattr(arguments, "runner", None) or config.get("defaultRunner")
    repositories = config.get("repositories") if isinstance(config.get("repositories"), list) else []
    remembered = preferences.profile().last_repository
    # A local Engine shares the terminal's filesystem, so the least surprising
    # task workspace is the directory where the person ran `engine run`.
    # Never make that assumption for a remote server: its filesystem may be
    # unrelated to the terminal client's.
    local_cwd = str(Path.cwd().resolve()) if is_local_server(server) else ""
    repository = getattr(arguments, "repository", None) or local_cwd or remembered or (
        repositories[0].get("path", "") if repositories and isinstance(repositories[0], dict) else ""
    )
    if not isinstance(agent, str) or not agent or not isinstance(runner, str) or not runner:
        raise RuntimeError("service does not advertise a default agent and runner")
    return agent, runner, str(repository)


def runner_choice_labels(server: str) -> dict[str, str]:
    """Return the configured runners as readable, unambiguous picker labels."""
    config = fetch_json(server, "/api/config")
    default = config.get("defaultRunner")
    entries = config.get("runners")
    runner_ids = [entry.get("id") for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)] if isinstance(entries, list) else []
    labels: dict[str, str] = {}
    for runner in runner_ids:
        label = runner.capitalize()
        if runner == default:
            label += " (default)"
        labels[label] = runner
    return labels


def is_local_server(server: str) -> bool:
    """Whether a server URL can safely use a client-side filesystem path."""
    return urlsplit(server).hostname in {"127.0.0.1", "localhost", "::1"}


def run(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            print(f"engine: {check.detail}", file=sys.stderr)
            return EXIT_UNHEALTHY
        agent, runner, repository = creation_defaults(server, preferences, arguments)
        thread = request_json(server, "/api/threads", {"agentId": agent, "runner": runner})
        if repository:
            thread = request_json(server, f"/api/threads/{thread['id']}/workspace", {"repository": repository})
        remember_thread(preferences, thread)
        print(f"Started {thread.get('title', 'task')} ({thread.get('id')})")
        return stream_run(server, f"/api/threads/{thread['id']}/runs", {"text": arguments.prompt, "runner": runner})
    except (ValueError, RuntimeError, KeyError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY


def pending_approvals(server: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for thread in load_threads(server, "all"):
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            continue
        messages = fetch_json(server, f"/api/threads/{thread_id}/messages")
        for approval in messages.get("approvals", []):
            if isinstance(approval, dict) and approval.get("status") == "pending":
                found.append({**approval, "threadId": thread_id, "threadTitle": thread.get("title", "Untitled")})
    return found


def render_approvals(approvals: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"approvals": approvals}, sort_keys=True))
        return
    if not approvals:
        print("No pending approvals.")
        return
    for approval in approvals:
        detail = approval.get("command") or approval.get("toolName") or approval.get("reason") or "approval requested"
        print(f"{approval.get('id')}  {approval.get('threadTitle')}\n  {detail}")


def decide(arguments: argparse.Namespace, preferences: Preferences, decision: str) -> int:
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            print(f"engine: {check.detail}", file=sys.stderr)
            return EXIT_UNHEALTHY
        match = next((item for item in pending_approvals(server) if item.get("id") == arguments.approval_id), None)
        if match is None:
            raise RuntimeError("pending approval not found")
        if decision == "cancel" and getattr(arguments, "reason", None):
            print(f"Rejecting: {arguments.reason}")
        request_json(server, f"/api/threads/{match['threadId']}/runs/current/approvals/{arguments.approval_id}", {"decision": decision})
        print("Approved." if decision == "accept" else "Rejected.")
        return EXIT_OK
    except (ValueError, RuntimeError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY


#: How long a connect request may take. The service reads its client ID from,
#: and saves the token to, the OS keychain, which can stop to ask for the
#: login password.
KEYCHAIN_TIMEOUT = 60.0
KEYCHAIN_EXPLAINED = (
    "OpenEngine keeps the access token in your system keychain, so it is stored "
    "encrypted rather than in a file and WorkOrders can push and open pull requests "
    "without asking you again. Your system may ask for your login password to allow it."
)


def connect(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            raise RuntimeError(check.detail)
        provider = arguments.provider
        if provider == "gh":
            post_empty(server, "/api/source-control/provider", {"provider": "gh-cli"})
            print("GitHub CLI selected. Run `gh auth login` if needed.")
            return EXIT_OK
        if provider == "slack":
            flow = request_json(server, "/api/slack/connect", {})
            authorization_url = str(flow["authorizationUrl"])
            print(f"Open {authorization_url} to connect Slack.")
            if arguments.open:
                webbrowser.open(authorization_url)
            deadline = time.monotonic() + 120.0
            while time.monotonic() < deadline:
                time.sleep(1.0)
                if fetch_json(server, "/api/slack/status").get("connected") is True:
                    print("Connected.")
                    return EXIT_OK
            raise RuntimeError("Slack authorization timed out")
        path = "/api/github/connect" if provider == "github" else "/api/gitlab/connect"
        body = {} if provider == "github" else {"origin": arguments.origin}
        print(KEYCHAIN_EXPLAINED)
        flow = request_json(server, path, body, timeout=KEYCHAIN_TIMEOUT)
        print(f"Open {flow['verificationUri']} and enter code: {flow['userCode']}")
        if arguments.open:
            webbrowser.open(str(flow["verificationUri"]))
        poll_path = "/api/github/connect/poll" if provider == "github" else "/api/gitlab/connect/poll"
        while True:
            time.sleep(float(flow.get("interval", 5)))
            result = request_json(server, poll_path, body, timeout=KEYCHAIN_TIMEOUT)
            if result.get("status") == "complete":
                post_empty(server, "/api/source-control/provider", {"provider": "github-oauth" if provider == "github" else "gitlab-oauth", **({"origin": arguments.origin} if provider == "gitlab" else {})})
                print("Connected.")
                return EXIT_OK
            print("Waiting for authorization…")
    except (ValueError, RuntimeError, KeyError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY


def settings(server: str, preferences: Preferences) -> None:
    """Offer the integrations collected by the web Settings panel."""
    progressive_connection_snapshot(server)
    provider = palette(["GitHub", "GitLab", "Slack", "Open web Settings", "Back"], "Settings: ")
    if provider == "GitHub":
        connect(argparse.Namespace(server=server, provider="github", origin="https://gitlab.com", open=True), preferences)
    elif provider == "GitLab":
        connect(argparse.Namespace(server=server, provider="gitlab", origin="https://gitlab.com", open=True), preferences)
    elif provider == "Slack":
        connect(argparse.Namespace(server=server, provider="slack", origin="https://gitlab.com", open=True), preferences)
    elif provider == "Open web Settings":
        webbrowser.open(server)
        print(f"Opened {server}; choose Settings in the sidebar.")


REVIEW_POLL_SECONDS = 2.0


@dataclass(frozen=True)
class ReviewTarget:
    """The change `engine review` hands to a workflow started in review."""

    repository: str
    ref: str
    pr_url: str
    task: str
    #: The pull request's branch a fix is pushed to; none when it cannot be.
    branch: str = ""


def pull_request(url: str) -> ChangeRequest | None:
    """The GitHub pull request a URL names, read the way the rest of the engine reads it."""
    request = change_request(url)
    return request if request is not None and request.kind == "pull" else None


def git_output(path: str | Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *arguments], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(arguments)} failed")
    return result.stdout.strip()


def gh_json(*arguments: str, cwd: str | Path | None = None) -> dict[str, Any]:
    if shutil.which("gh") is None:
        raise RuntimeError("the GitHub CLI (gh) is not on PATH")
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh {' '.join(arguments)} failed")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(f"gh {' '.join(arguments)} did not return a JSON object")
    return payload


def review_repository(request: ChangeRequest, server: str, config: dict[str, Any]) -> str:
    """The service-side checkout of the pull request's repository, from `[repos]` or this directory."""
    repositories = config.get("repositories") if isinstance(config.get("repositories"), list) else []
    for repository in repositories:
        if isinstance(repository, dict) and str(repository.get("name", "")).casefold() == request.path.casefold():
            return str(repository.get("path", ""))
    if is_local_server(server):
        try:
            root = git_output(Path.cwd(), "rev-parse", "--show-toplevel")
            origin = git_output(root, "remote", "get-url", "origin")
        except RuntimeError:
            origin = ""
        # The whole project, host included: `myowner/repo` is not `owner/repo`.
        if remote_project(origin) == request.project:
            return root
    raise RuntimeError(f"no checkout of {request.path} is configured on this service; add it under [repos] in engine.toml")


def review_target(target: str, server: str, config: dict[str, Any]) -> ReviewTarget:
    """Resolve a pull request URL or a local path into what the review checks out."""
    if target.startswith(("http://", "https://")):
        request = pull_request(target)
        if request is None:
            raise RuntimeError("expected a GitHub pull request URL such as https://github.com/owner/repo/pull/1")
        pull = gh_json("pr", "view", target, "--json", "url,title,headRefName,isCrossRepository")
        if pull.get("isCrossRepository"):
            # A fork's code would run our agents, and a fix has no remote to go back to.
            raise RuntimeError(
                "pull requests from forks are not reviewed: their code would run the review's agents, "
                "and a fix could not be pushed back; check the branch out and run engine review on the path"
            )
        branch = str(pull["headRefName"])
        url = str(pull.get("url") or target)
        return ReviewTarget(
            review_repository(request, server, config), f"origin/{branch}", url,
            f"Review pull request {url}: {pull.get('title', '')}", branch,
        )
    if not is_local_server(server):
        raise RuntimeError("a remote service cannot see local paths; give a pull request URL instead")
    root = git_output(Path(target).expanduser().resolve(), "rev-parse", "--show-toplevel")
    branch = git_output(root, "rev-parse", "--abbrev-ref", "HEAD")
    commit = git_output(root, "rev-parse", "HEAD")
    if git_output(root, "status", "--porcelain"):
        print("engine: uncommitted changes are not reviewed; commit them to include them.", file=sys.stderr)
    try:
        pull = gh_json("pr", "view", "--json", "url,headRefName,isCrossRepository", cwd=root)
    except (RuntimeError, json.JSONDecodeError):
        pull = {}
    return ReviewTarget(
        root, commit, str(pull.get("url") or ""),
        f"Review the commits on branch {branch} (at {commit[:12]}) that are not on the repository's default branch.",
        # A fork's branch is not on `origin`, the only remote a workspace has.
        "" if pull.get("isCrossRepository") else str(pull.get("headRefName") or ""),
    )


def review_workflow(config: dict[str, Any]) -> dict[str, Any]:
    """The first offered workflow that can start in the review state."""
    for workflow in config.get("workflows") or []:
        inputs = workflow.get("inputs") if isinstance(workflow, dict) else None
        for item in inputs or []:
            if isinstance(item, dict) and item.get("name") == STATE_INPUT and WorkState.REVIEW in (item.get("choices") or []):
                return workflow
    raise RuntimeError("no workflow on this service can start in review")


def start_review(server: str, config: dict[str, Any], target: ReviewTarget) -> str:
    workflow = review_workflow(config)
    declared = {item.get("name") for item in workflow.get("inputs") or [] if isinstance(item, dict)}
    run = request_json(server, "/api/runs", {
        "prompt": target.task, "repository": target.repository, "workflowId": workflow["id"],
        "inputs": review_inputs(declared, ref=target.ref, pr_url=target.pr_url, branch=target.branch),
    })
    return str(run["runId"])


def graph_run(server: str, run_id: str) -> dict[str, Any]:
    return fetch_json(server, f"/graph/api/runs/{run_id}?includeValues=true")


def step_names(server: str, graph_id: str) -> dict[str, str] | None:
    """Each node's display name, so progress reads `Review (Bugs)` rather than a node id; None to retry next poll."""
    try:
        graph = fetch_json(server, f"/graph/api/graphs/{graph_id}")
    except RuntimeError:
        return None
    return {
        str(node["nodeId"]): str(node.get("name") or node["nodeId"])
        for node in graph.get("nodes") or [] if isinstance(node, dict) and node.get("nodeId")
    }


def review_steps(run: dict[str, Any], names: dict[str, str]) -> dict[str, str]:
    """The run's executing steps, by execution id, with their display names."""
    return {
        str(item.get("executionId")): names.get(str(item.get("nodeId")), str(item.get("nodeId")))
        for item in run.get("activeExecutions") or [] if isinstance(item, dict)
    }


def review_spinner(steps: dict[str, str]) -> TerminalSpinner:
    return TerminalSpinner(f"Reviewing: {', '.join(steps.values())}" if steps else "Reviewing")


def wait_for_triage(server: str, run_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Watch the run until it asks for findings to fix, or ends, saying each step as it starts and ends."""
    spinner = review_spinner({})
    spinner.start()
    answered: set[str] = set()
    names: dict[str, str] | None = None
    steps: dict[str, str] = {}
    try:
        while True:
            run = graph_run(server, run_id)
            if names is None and run.get("graphId"):
                names = step_names(server, str(run["graphId"]))
            current = review_steps(run, names or {})
            if current != steps:
                spinner.stop()
                for execution, name in steps.items():
                    if execution not in current:
                        print(f"✓ {name}", file=sys.stderr)
                for execution, name in current.items():
                    if execution not in steps:
                        print(f"→ {name}", file=sys.stderr)
                steps = current
                spinner = review_spinner(steps)
                spinner.start()
            pending = [item for item in run.get("pendingApprovals") or [] if isinstance(item, dict)]
            triage = next((item for item in pending if item.get("toolName") == TRIAGE_TOOL), None)
            if triage is not None or run.get("status") in {"completed", "failed"}:
                return run, triage
            for approval in pending:
                if approval.get("approvalId") in answered:
                    continue
                answered.add(str(approval.get("approvalId")))
                spinner.stop()
                print(f"Approval required: {approval.get('command') or approval.get('toolName') or approval.get('reason')}")
                action = palette(["Approve", "Reject", "Defer"], "Decision: ") if sys.stdin.isatty() else None
                if action in {"Approve", "Reject"}:
                    request_json(server, f"/graph/api/runs/{run_id}/approvals/{approval['approvalId']}", {"decision": "accept" if action == "Approve" else "cancel"})
                else:
                    print("Answer it in the web UI; still watching the review.")
                spinner = review_spinner(steps)
                spinner.start()
            time.sleep(REVIEW_POLL_SECONDS)
    finally:
        spinner.stop()


def triage_findings(server: str, run: dict[str, Any], triage: dict[str, Any]) -> list[dict[str, Any]]:
    """The findings the triage node offers, read from where its topology says."""
    graph = fetch_json(server, f"/graph/api/graphs/{run['graphId']}")
    node = next((item for item in graph.get("nodes") or [] if item.get("nodeId") == triage.get("nodeId")), {})
    findings = (run.get("values") or {}).get(node.get("findingsKey") or "")
    return [item for item in findings if isinstance(item, dict)] if isinstance(findings, list) else []


def finding_location(finding: dict[str, Any]) -> str:
    file = finding.get("file")
    return f"{file}:{finding['line']}" if file and finding.get("line") else str(file or "")


def render_findings(findings: list[dict[str, Any]]) -> None:
    if not findings:
        print("No findings survived review.")
        return
    print(f"Findings ({len(findings)})")
    for index, finding in enumerate(findings, 1):
        facet = f"[{finding['facet']}] " if finding.get("facet") else ""
        print(f"{index}. {facet}{' '.join(str(finding.get('tagline', '')).split())}")
        for line in str(finding.get("description", "")).splitlines():
            print(f"   {line}")
        if location := finding_location(finding):
            print(f"   \x1b[2m{location}\x1b[0m")
    print()


def post_findings(pr_url: str, findings: list[dict[str, Any]]) -> int:
    """Post each finding with `gh`, inline where it has a line. Returns failures."""
    request = pull_request(pr_url)
    if request is None:
        raise RuntimeError(f"{pr_url} is not a GitHub pull request URL")
    head = str(gh_json("pr", "view", pr_url, "--json", "headRefOid").get("headRefOid") or "")
    failures = 0
    for finding in findings:
        # Worded as the reranker would have posted it.
        body = finding_comment(
            str(finding.get("tagline", "")), str(finding.get("description", "")),
            agent=str(finding.get("agent") or ""), facet=str(finding.get("facet") or ""),
        )
        line = finding.get("line")
        # Only a real line number: `gh -F` reads a value starting with `@` as a file.
        if head and finding.get("file") and isinstance(line, int) and not isinstance(line, bool) and line > 0:
            inline = subprocess.run([
                "gh", "api", "--hostname", request.host, "--method", "POST",
                f"repos/{request.path}/pulls/{request.number}/comments",
                "-f", f"body={body}", "-f", f"commit_id={head}", "-f", f"path={finding['file']}",
                "-F", f"line={line}", "-f", "side=RIGHT",
            ], capture_output=True, text=True)
            if inline.returncode == 0:
                continue
        # A line outside the diff cannot take an inline comment; say it generally.
        general = subprocess.run(["gh", "pr", "comment", pr_url, "--body", body], capture_output=True, text=True)
        if general.returncode != 0:
            failures += 1
            print(f"engine: could not post {finding.get('tagline')!r}: {general.stderr.strip()}", file=sys.stderr)
    return failures


def send_fixes(server: str, run_id: str, triage: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    """Steer the chosen findings to triage, then accept, the way a human review's note is sent."""
    request_json(server, f"/graph/api/runs/{run_id}/steering", {"message": json.dumps(findings), "node": triage["nodeId"]})
    request_json(server, f"/graph/api/runs/{run_id}/approvals/{triage['approvalId']}", {"decision": "accept"})


def choose_fixes(server: str, run_id: str, triage: dict[str, Any], findings: list[dict[str, Any]], pr_url: str) -> bool:
    """Offer Fix this / Fix all / Post as comments. True when fixes were sent."""
    selected: set[int] = set()
    cursor = 0
    while True:
        labels: dict[str, int] = {}
        descriptions: dict[str, str] = {}
        for index, finding in enumerate(findings):
            label = f"{'✓' if index in selected else '○'} Fix this · {index + 1}. {' '.join(str(finding.get('tagline', '')).split())}"
            labels[label] = index
            descriptions[label] = finding_location(finding)
        actions = [*labels]
        if selected:
            actions.append(f"Fix selected ({len(selected)})")
        if findings:
            actions.append("Fix all")
            if pr_url:
                actions.append("Post as comments")
        actions.append("Finish")
        descriptions.update({
            "Fix all": "Send every finding back to the implementer",
            "Post as comments": f"Post {'the selected' if selected else 'every'} finding on {pr_url} with gh",
            "Finish": "End the review without fixing anything",
        })
        choice = palette(actions, "Review: ", descriptions=descriptions, selected=cursor)
        if choice in labels:
            # Enter picks a finding for fixing, or puts it back, and stays on it.
            cursor = labels[choice]
            selected ^= {cursor}
        elif choice is not None and choice.startswith("Fix selected"):
            send_fixes(server, run_id, triage, [findings[index] for index in sorted(selected)])
            return True
        elif choice == "Fix all":
            send_fixes(server, run_id, triage, findings)
            return True
        elif choice == "Post as comments":
            chosen = [findings[index] for index in sorted(selected)] or findings
            failures = post_findings(pr_url, chosen)
            print(f"Posted {len(chosen) - failures} of {len(chosen)} findings on {pr_url}.")
        elif choice == "Finish":
            request_json(server, f"/graph/api/runs/{run_id}/approvals/{triage['approvalId']}", {"decision": "cancel"})
            print("Review finished.")
            return False
        else:
            print(f"Detached; the review waits for your choice at {server}/runs/{run_id}.")
            return False


def review(arguments: argparse.Namespace, preferences: Preferences) -> int:
    """Start a workflow in review on a local change or a pull request, then triage it."""
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            raise RuntimeError(check.detail)
        config = fetch_json(server, "/api/config")
        target = review_target(arguments.target, server, config)
        run_id = start_review(server, config, target)
        print(f"Reviewing {target.pr_url or target.repository} ({run_id})")
        while True:
            run, triage = wait_for_triage(server, run_id)
            if triage is None:
                if run.get("status") == "failed":
                    raise RuntimeError(f"review failed: {run.get('error')}")
                print("Review finished.")
                return EXIT_OK
            findings = triage_findings(server, run, triage)
            if arguments.json:
                print(json.dumps({"runId": run_id, "prUrl": target.pr_url, "findings": findings}, sort_keys=True))
                return EXIT_OK
            render_findings(findings)
            if not sys.stdin.isatty():
                print(f"Choose findings to fix at {server}/runs/{run_id}.")
                return EXIT_OK
            if not choose_fixes(server, run_id, triage, findings, target.pr_url):
                return EXIT_OK
            print("Fixing; the change is reviewed again when the fix is done.")
    except KeyboardInterrupt:
        print("\nDetached; the service-side review continues.")
        return EXIT_OK
    except (ValueError, RuntimeError, KeyError, json.JSONDecodeError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY


COMMAND_DESCRIPTIONS = {
    "/help": "Show available commands",
    "/status": "Check whether the OpenEngine service is ready",
    "/threads": "Browse conversations on this service",
    "/new": "Start a new work order",
    "/approvals": "Review pending terminal decisions",
    "/review": "Review a local change or a pull request",
    "/settings": "Manage GitHub, GitLab, and Slack connections",
    "/web": "Open the OpenEngine web interface",
    "/quit": "Exit the CLI",
}


def palette_lines(
    options: list[str], prompt: str, query: str, selected: int,
    descriptions: dict[str, str] | None = None,
) -> list[str]:
    """Render one palette frame without taking over the terminal screen."""
    width = shutil.get_terminal_size(fallback=(100, 24)).columns
    command_width = min(24, max(14, width // 3)) if descriptions is None else min(
        max((len(option) for option in options), default=14), max(14, width * 2 // 3),
    )
    lines = [f"\x1b[1m{prompt}{query}\x1b[0m", "─" * max(1, width)]
    for index, option in enumerate(options):
        description = (COMMAND_DESCRIPTIONS if descriptions is None else descriptions).get(option, "")
        available = max(0, width - command_width - 3)
        if len(description) > available:
            description = description[: max(0, available - 1)] + "…"
        marker = "›" if index == selected else " "
        style = "\x1b[38;5;111m" if index == selected else "\x1b[2m"
        # A wrapped row would throw off the redraw, which counts lines.
        shown = option if len(option) <= command_width else option[: command_width - 1] + "…"
        lines.append(f"{style}{marker} {shown:<{command_width}} {description}\x1b[0m")
    if not options:
        lines.append("\x1b[2m  No matching commands\x1b[0m")
    return lines


def draw_palette(lines: list[str], previous_rows: int) -> int:
    """Redraw only the palette, preserving the terminal transcript above it."""
    if previous_rows:
        print(f"\x1b[{previous_rows}F\x1b[J", end="")
    else:
        print("\r\x1b[2K", end="")
    print("\n".join(lines), flush=True)
    return len(lines)


def dismiss_palette(rows: int, prompt: str, value: str = "") -> None:
    """Remove the transient picker and retain the chosen command as history."""
    print(f"\x1b[{rows}F\r\x1b[2K{prompt}{value}\n\x1b[J", end="", flush=True)


def palette(
    options: list[str], prompt: str, *, initial_query: str = "",
    descriptions: dict[str, str] | None = None, selected: int = 0,
) -> str | None:
    """A searchable, inline arrow-key picker without a UI dependency."""
    query = initial_query
    rendered_rows = 0
    while True:
        matches = [option for option in options if query.casefold() in option.casefold()]
        if matches:
            selected = min(selected, len(matches) - 1)
        else:
            selected = 0
        rendered_rows = draw_palette(palette_lines(matches, prompt, query, selected, descriptions), rendered_rows)
        key = read_key()
        if key == "enter":
            choice = matches[selected] if matches else None
            dismiss_palette(rendered_rows, prompt, choice or query)
            return choice
        if key == "quit":
            dismiss_palette(rendered_rows, prompt, "/quit")
            return "/quit"
        if key == "escape":
            dismiss_palette(rendered_rows, prompt)
            return None
        if key == "up" and matches:
            selected = (selected - 1) % len(matches)
        elif key == "down" and matches:
            selected = (selected + 1) % len(matches)
        elif key == "backspace":
            query = query[:-1]
        elif len(key) == 1 and key.isprintable():
            query += key


def prompt_line(prompt: str, *, initial: str = "") -> str | None:
    """Read a one-line value with the picker’s Escape-to-cancel behavior."""
    characters = list(initial)
    print(f"{prompt}{initial}", end="", flush=True)
    while True:
        key = read_key()
        if key == "escape":
            print("\r\x1b[2K", end="", flush=True)
            return None
        if key == "enter":
            print()
            return "".join(characters)
        if key == "backspace":
            if characters:
                characters.pop()
                print("\b \b", end="", flush=True)
        elif len(key) == 1 and key.isprintable():
            characters.append(key)
            print(key, end="", flush=True)


def read_key() -> str:
    if os.name == "nt":
        import msvcrt

        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        return {"\r": "enter", "\x1a": "quit", "\x1b": "escape", "\x08": "backspace"}.get(key, key)
    import select
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        # Do not mix TextIOWrapper reads with select/os.read below: the wrapper
        # can prefetch `[` and `A`/`B` from an arrow sequence, making select
        # incorrectly conclude that lone Escape was pressed.
        key = os.read(descriptor, 1).decode(errors="ignore")
        if key == "\x1b":
            # Escape is also the first byte of an arrow-key sequence. Waiting
            # for two more bytes here made a lone Escape feel like it needed
            # extra key presses before a menu could go back.
            ready, _, _ = select.select([descriptor], [], [], 0.03)
            if not ready:
                return "escape"
            first = os.read(descriptor, 1).decode(errors="ignore")
            if first != "[":
                return "escape"
            ready, _, _ = select.select([descriptor], [], [], 0.03)
            if not ready:
                return "escape"
            second = os.read(descriptor, 1).decode(errors="ignore")
            return {"A": "up", "B": "down"}.get(second, "escape")
        return {"\r": "enter", "\n": "enter", "\x1a": "quit", "\x7f": "backspace"}.get(key, key)
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def interactive(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server, check = read_service(arguments, preferences)
    except ValueError as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_USAGE
    if not check.ok:
        print(f"engine: {check.detail}", file=sys.stderr)
        return EXIT_UNHEALTHY
    print(f"OpenEngine workbench — {server}. Type / for commands.")
    render_startup_dashboard(server)
    commands = [
        "/help",
        "/status",
        "/threads",
        "/new",
        "/approvals",
        "/review",
        "/settings",
        "/web",
        "/quit",
    ]
    palette_open = False
    while True:
        if palette_open:
            command = palette(commands, "engine> ", initial_query="/")
            palette_open = False
        else:
            # Read the first key directly so `/` opens the picker without making
            # someone press Enter first. Any remaining keys, including a pasted
            # `/status`, are consumed by the fuzzy picker as its search query.
            print("engine> ", end="", flush=True)
            key = read_key()
            if key == "quit":
                print()
                return EXIT_OK
            if key in {"up", "down", "escape", "backspace"}:
                # Navigation keys only apply inside a picker. Do not render
                # their internal names as prompt text at the top level.
                print("\r\x1b[2K", end="", flush=True)
                continue
            if key != "/":
                if len(key) != 1 or not key.isprintable():
                    print("\r\x1b[2K", end="", flush=True)
                    continue
                prompt = prompt_line("", initial=key)
                if prompt and prompt.strip():
                    run(argparse.Namespace(server=server, prompt=prompt.strip(), agent=None, runner=None, repository=None), preferences)
                continue
            command = palette(commands, "engine> ", initial_query="/")
        # Escape dismisses this top-level picker and returns to `engine>`.
        # Nested pickers use the same `None` result to return to their parent.
        if command is None:
            continue
        if command == "/help":
            print("/status  service readiness\n/settings  integration settings\n/threads  inspect work orders\n/new  start a new work order\n/approvals  pending decisions\n/review  review a change or pull request\n/web  open the web UI\n/quit  exit")
        elif command == "/status":
            status(argparse.Namespace(server=server, json=False), preferences)
        elif command == "/threads":
            thread_filter = palette(["Active", "All", "Archived"], "Threads: ")
            if thread_filter is None:
                continue
            values = load_threads(server, thread_filter.casefold())
            summaries = {str(item.get("id", "")): thread_summary(server, item) for item in values}
            render_threads(values, False, summaries)
            choices = thread_choice_labels(values, summaries)
            selected = palette(list(choices), "Open thread: ") if choices else None
            if selected:
                open_thread(server, choices[selected], preferences)
        elif command == "/new":
            try:
                runner_choices = runner_choice_labels(server)
            except RuntimeError as error:
                print(f"engine: {error}", file=sys.stderr)
                continue
            selected_runner = palette(list(runner_choices), "Runner: ") if runner_choices else None
            if selected_runner is None:
                continue
            prompt = (prompt_line("Describe the work: ") or "").strip()
            if prompt:
                run(argparse.Namespace(server=server, prompt=prompt, agent=None, runner=runner_choices[selected_runner], repository=None), preferences)
        elif command == "/approvals":
            pending = pending_approvals(server)
            render_approvals(pending, False)
            choices = [f"{item.get('id')} — {item.get('threadTitle')}" for item in pending]
            selected = palette(choices, "Approval: ") if choices else None
            if selected:
                approval_id = selected.split(" — ", 1)[0]
                action = palette(["Approve", "Reject", "View details", "Back"], "Decision: ")
                item = next(item for item in pending if item.get("id") == approval_id)
                if action == "View details":
                    print(json.dumps(item, indent=2, sort_keys=True))
                elif action == "Approve":
                    decide(argparse.Namespace(server=server, approval_id=approval_id), preferences, "accept")
                elif action == "Reject":
                    reason = prompt_line("Reason: ")
                    if reason is not None:
                        decide(argparse.Namespace(server=server, approval_id=approval_id, reason=reason.strip() or "Rejected in terminal"), preferences, "cancel")
        elif command == "/review":
            target = prompt_line("Review (path or pull request URL): ", initial=".")
            if target and target.strip():
                review(argparse.Namespace(server=server, target=target.strip(), json=False), preferences)
        elif command == "/settings":
            settings(server, preferences)
        elif command == "/web":
            webbrowser.open(server)
            print(f"Opened {server}")
        elif command == "/quit":
            return EXIT_OK


def workbench(arguments: argparse.Namespace, preferences: Preferences) -> int:
    """The full-screen WorkOrder workbench `engine` opens with no command."""
    from engine.apps.cli.tui import App, ServiceClient, run_workbench

    try:
        server, check = read_service(arguments, preferences)
    except ValueError as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_USAGE
    if not check.ok:
        print(f"engine: {check.detail}", file=sys.stderr)
        return EXIT_UNHEALTHY
    app = App(
        ServiceClient(server, fetch_json, request_json),
        disconnected=bool(getattr(arguments, "disconnected", False)),
        # As `engine run` does: only a local service shares this filesystem.
        local_repository=str(Path.cwd().resolve()) if is_local_server(server) else "",
    )
    return run_workbench(app)


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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="engine", description=__doc__)
    result.add_argument("--version", action="version", version=version("engine-cli"))
    result.add_argument("--server", metavar="URL", help="override the configured OpenEngine service")
    result.add_argument(
        "--disconnected", action="store_true",
        help="start new WorkOrders in disconnected mode: no push, pull request, or comments",
    )
    commands = result.add_subparsers(dest="command")
    for name, help_text in (("status", "show OpenEngine service identity and readiness"), ("doctor", "diagnose local prerequisites and service access")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--server", metavar="URL", help="override the configured OpenEngine service")
        command.add_argument("--json", action="store_true", help="emit a stable machine-readable report")
    connection = commands.add_parser("connect", help="connect shared source control or Slack")
    connection.add_argument("provider", choices=("gh", "github", "gitlab", "slack"))
    connection.add_argument("--server", metavar="URL")
    connection.add_argument("--origin", default="https://gitlab.com")
    connection.add_argument("--open", action="store_true")
    reviewing = commands.add_parser("review", help="review a local change or a pull request, then choose what to fix")
    reviewing.add_argument("target", nargs="?", default=".", help="a repository path (default: current directory) or a pull request URL")
    reviewing.add_argument("--server", metavar="URL", help="override the configured OpenEngine service")
    reviewing.add_argument("--json", action="store_true", help="print the findings as JSON and leave the review waiting")
    onboarding.add_parser(commands)
    daemon.add_parser(commands)
    palette_command = commands.add_parser("palette", help="the line-based command palette")
    palette_command.add_argument("--server", metavar="URL", help="override the configured OpenEngine service")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    preferences = load_preferences()
    if arguments.command is None:
        if argv is None and sys.stdin.isatty() and sys.stdout.isatty():
            return workbench(arguments, preferences)
        parser().print_help()
        return EXIT_USAGE
    if arguments.command == "palette":
        return interactive(arguments, preferences)
    if arguments.command == "status":
        return status(arguments, preferences)
    if arguments.command == "doctor":
        return doctor(arguments, preferences)
    if arguments.command == "connect":
        return connect(arguments, preferences)
    if arguments.command == "review":
        return review(arguments, preferences)
    if arguments.command == "init":
        return onboarding.main(arguments)
    if arguments.command == "daemon":
        return daemon.main(arguments)
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())

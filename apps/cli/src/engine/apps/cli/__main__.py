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
import webbrowser
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


def request_json(server: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        f"{server}{path}", data=json.dumps(body).encode(), method="POST",
        headers=request_headers({"Accept": "application/json", "Content-Type": "application/json"}),
    )
    try:
        with urlopen(request, timeout=10.0) as response:
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


def stream_run(server: str, path: str, body: dict[str, Any] | None = None) -> int:
    request = Request(
        f"{server}{path}", data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET",
        headers=request_headers({"Accept": "application/x-ndjson", **({"Content-Type": "application/json"} if body is not None else {})}),
    )
    try:
        with urlopen(request, timeout=30.0) as response:
            if response.status == 204:
                print("No active run.")
                return EXIT_OK
            for line in response:
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("type") == "content":
                    print(content_text(event.get("content")), end="\r", flush=True)
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
                            request_json(
                                server,
                                f"/api/threads/{thread_id}/runs/current/approvals/{approval['id']}",
                                {"decision": "accept" if action == "Approve" else "cancel"},
                            )
                            print("Approval sent; continuing stream.")
                    else:
                        print(f"Run `engine approve {approval['id']}` or `engine reject {approval['id']} --reason …`.")
                elif event.get("type") == "error":
                    print(f"\nengine: {event.get('error')}", file=sys.stderr)
                    return EXIT_UNHEALTHY
                elif event.get("type") == "done":
                    text = content_text(event.get("content"))
                    if text:
                        print(f"\n{text}")
                    return EXIT_OK
    except KeyboardInterrupt:
        print("\nDetached; the service-side run continues.")
        return EXIT_OK
    except HTTPError as error:
        print(f"engine: server returned HTTP {error.code}", file=sys.stderr)
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        print(f"engine: stream disconnected: {error}", file=sys.stderr)
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


def render_threads(threads: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps({"threads": threads}, sort_keys=True))
        return
    if not threads:
        print("No threads.")
        return
    for thread in threads:
        archived = " archived" if thread.get("archived") else ""
        repository = thread.get("workspaceRoot") or "no repository attached"
        print(f"{thread.get('id', '?')}  {thread.get('title', 'Untitled')} [{repository}]{archived}")


def threads(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            render([check], arguments.json, {"server": server})
            return EXIT_UNHEALTHY
        values = load_threads(server, arguments.filter)
    except (ValueError, RuntimeError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY
    render_threads(values, arguments.json)
    return EXIT_OK


def remember_thread(preferences: Preferences, thread: dict[str, Any]) -> None:
    profiles = dict(preferences.profiles or {})
    current = profiles.get(preferences.selected_profile, Profile())
    profiles[preferences.selected_profile] = Profile(
        current.server,
        str(thread.get("workspaceRoot") or current.last_repository),
        str(thread.get("id") or current.last_task),
    )
    save_preferences(Preferences(preferences.selected_profile, profiles))


def render_thread(thread: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(thread, sort_keys=True))
        return
    print(f"{thread.get('title', 'Untitled')} ({thread.get('id', '?')})")
    print(f"Repository: {thread.get('workspaceRoot') or 'not attached'}")
    print(f"Runner: {thread.get('runner', 'unknown')}")
    print(f"State: {thread.get('phase', 'archived' if thread.get('archived') else 'idle')}")
    current = thread.get("currentRun")
    print(f"Current run: {current.get('id') if isinstance(current, dict) else 'none'}")
    previous = thread.get("previousRuns")
    print(f"Previous runs: {len(previous) if isinstance(previous, list) else 0}")
    print(f"Pending approval: {'yes' if thread.get('pendingApproval') else 'no'}")


def task(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            render([check], arguments.json, {"server": server})
            return EXIT_UNHEALTHY
        thread = fetch_json(server, f"/api/threads/{arguments.thread_id}")
        remember_thread(preferences, thread)
    except (ValueError, RuntimeError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY
    render_thread(thread, arguments.json)
    return EXIT_OK


def creation_defaults(server: str, preferences: Preferences, arguments: argparse.Namespace) -> tuple[str, str, str]:
    config = fetch_json(server, "/api/config")
    agent = getattr(arguments, "agent", None) or config.get("defaultAgent")
    runner = getattr(arguments, "runner", None) or config.get("defaultRunner")
    repositories = config.get("repositories") if isinstance(config.get("repositories"), list) else []
    remembered = preferences.profile().last_repository
    repository = getattr(arguments, "repository", None) or remembered or (
        repositories[0].get("path", "") if repositories and isinstance(repositories[0], dict) else ""
    )
    if not isinstance(agent, str) or not agent or not isinstance(runner, str) or not runner:
        raise RuntimeError("service does not advertise a default agent and runner")
    return agent, runner, str(repository)


def run(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            print(f"engine: {check.detail}", file=sys.stderr)
            return EXIT_UNHEALTHY
        agent, runner, repository = creation_defaults(server, preferences, arguments)
        thread = request_json(server, "/api/threads", {"agentId": agent, "runner": runner})
        if repository:
            thread["workspaceRoot"] = repository
        remember_thread(preferences, thread)
        print(f"Started {thread.get('title', 'task')} ({thread.get('id')})")
        return stream_run(server, f"/api/threads/{thread['id']}/runs", {"text": arguments.prompt, "runner": runner})
    except (ValueError, RuntimeError, KeyError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY


def resume(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            print(f"engine: {check.detail}", file=sys.stderr)
            return EXIT_UNHEALTHY
        thread = fetch_json(server, f"/api/threads/{arguments.thread_id}")
        remember_thread(preferences, thread)
    except (ValueError, RuntimeError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY
    return stream_run(server, f"/api/threads/{arguments.thread_id}/runs/current")


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


def approvals(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            print(f"engine: {check.detail}", file=sys.stderr)
            return EXIT_UNHEALTHY
        render_approvals(pending_approvals(server), arguments.json)
        return EXIT_OK
    except (ValueError, RuntimeError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY


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
        task(argparse.Namespace(server=server, thread_id=match["threadId"], json=False), preferences)
        return EXIT_OK
    except (ValueError, RuntimeError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY


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
        path = "/api/github/connect" if provider == "github" else "/api/gitlab/connect"
        body = {} if provider == "github" else {"origin": arguments.origin}
        flow = request_json(server, path, body)
        print(f"Open {flow['verificationUri']} and enter code: {flow['userCode']}")
        if arguments.open:
            webbrowser.open(str(flow["verificationUri"]))
        poll_path = "/api/github/connect/poll" if provider == "github" else "/api/gitlab/connect/poll"
        while True:
            time.sleep(float(flow.get("interval", 5)))
            result = request_json(server, poll_path, body)
            if result.get("status") == "complete":
                post_empty(server, "/api/source-control/provider", {"provider": "github-oauth" if provider == "github" else "gitlab-oauth", **({"origin": arguments.origin} if provider == "gitlab" else {})})
                print("Connected.")
                return EXIT_OK
            print("Waiting for authorization…")
    except (ValueError, RuntimeError, KeyError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY


def repo(arguments: argparse.Namespace, preferences: Preferences) -> int:
    try:
        server, check = read_service(arguments, preferences)
        if not check.ok:
            raise RuntimeError(check.detail)
        repositories = fetch_json(server, "/api/config").get("repositories", [])
        selected = next((item for item in repositories if isinstance(item, dict) and arguments.repository in {item.get("name"), item.get("path")}), None)
        if selected is None:
            raise RuntimeError("repository is not offered by the service")
        profiles = dict(preferences.profiles or {})
        current = profiles.get(preferences.selected_profile, Profile())
        profiles[preferences.selected_profile] = Profile(current.server, str(selected["path"]), current.last_task)
        save_preferences(Preferences(preferences.selected_profile, profiles))
        print(f"Repository: {selected['name']} ({selected['path']})")
        return EXIT_OK
    except (ValueError, RuntimeError, KeyError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_UNHEALTHY


def palette(options: list[str], prompt: str) -> str | None:
    """A tiny searchable, arrow-key/Enter picker without a UI dependency."""
    query = ""
    selected = 0
    while True:
        matches = [option for option in options if query.casefold() in option.casefold()]
        if matches:
            selected = min(selected, len(matches) - 1)
        else:
            selected = 0
        print("\x1b[2J\x1b[H" + prompt + query)
        for index, option in enumerate(matches):
            print(("› " if index == selected else "  ") + option)
        key = read_key()
        if key == "enter":
            return matches[selected] if matches else None
        if key == "escape":
            return None
        if key == "up" and matches:
            selected = (selected - 1) % len(matches)
        elif key == "down" and matches:
            selected = (selected + 1) % len(matches)
        elif key == "backspace":
            query = query[:-1]
        elif len(key) == 1 and key.isprintable():
            query += key


def read_key() -> str:
    if os.name == "nt":
        import msvcrt

        key = msvcrt.getwch()
        if key in {"\x00", "\xe0"}:
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "")
        return {"\r": "enter", "\x1b": "escape", "\x08": "backspace"}.get(key, key)
    import termios
    import tty

    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setraw(descriptor)
        key = sys.stdin.read(1)
        if key == "\x1b":
            suffix = sys.stdin.read(2)
            return {"[A": "up", "[B": "down"}.get(suffix, "escape")
        return {"\r": "enter", "\n": "enter", "\x7f": "backspace"}.get(key, key)
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
    while True:
        line = input("engine> ").strip()
        if not line:
            continue
        if line != "/":
            print("Use / to open the command palette. Type /quit to exit.")
            continue
        command = palette(["/help", "/status", "/threads", "/new", "/approvals", "/setup", "/connect", "/web", "/quit"], "Command: ")
        if command in {None, "/help"}:
            print("/status  service readiness\n/threads  inspect conversations\n/new  start a task\n/approvals  pending decisions\n/web  open the web UI\n/quit  exit")
        elif command == "/status":
            status(argparse.Namespace(server=server, json=False), preferences)
        elif command == "/threads":
            thread_filter = palette(["Active", "All", "Archived"], "Threads: ")
            if thread_filter is None:
                continue
            values = load_threads(server, thread_filter.casefold())
            render_threads(values, False)
            choices = [f"{item.get('title', 'Untitled')} — {item.get('id', '?')}" for item in values]
            selected = palette(choices, "Open thread: ") if choices else None
            if selected:
                thread_id = selected.rsplit(" — ", 1)[-1]
                resume(argparse.Namespace(server=server, thread_id=thread_id), preferences)
        elif command == "/new":
            prompt = input("Task: ").strip()
            if prompt:
                run(argparse.Namespace(server=server, prompt=prompt, agent=None, runner=None, repository=None), preferences)
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
                    reason = input("Reason: ").strip() or "Rejected in terminal"
                    decide(argparse.Namespace(server=server, approval_id=approval_id, reason=reason), preferences, "cancel")
        elif command in {"/setup", "/connect"}:
            provider = palette(["gh", "github", "gitlab", "Skip"], "Provider: ")
            if provider and provider != "Skip":
                connect(argparse.Namespace(server=server, provider=provider, origin="https://gitlab.com", open=True), preferences)
        elif command == "/web":
            webbrowser.open(server)
            print(f"Opened {server}")
        elif command == "/quit":
            return EXIT_OK


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
    thread_list = commands.add_parser("threads", help="list service conversations")
    thread_list.add_argument("--server", metavar="URL", help="override the configured OpenEngine service")
    thread_list.add_argument("--json", action="store_true")
    filters = thread_list.add_mutually_exclusive_group()
    filters.add_argument("--all", dest="filter", action="store_const", const="all", default="active")
    filters.add_argument("--archived", dest="filter", action="store_const", const="archived")
    task_command = commands.add_parser("task", help="inspect one service conversation")
    task_command.add_argument("thread_id")
    task_command.add_argument("--server", metavar="URL", help="override the configured OpenEngine service")
    task_command.add_argument("--json", action="store_true")
    run_command = commands.add_parser("run", help="create a conversation and stream its task")
    run_command.add_argument("prompt")
    run_command.add_argument("--server", metavar="URL", help="override the configured OpenEngine service")
    run_command.add_argument("--agent")
    run_command.add_argument("--runner")
    run_command.add_argument("--repository")
    resume_command = commands.add_parser("resume", help="reconnect to a conversation's current run")
    resume_command.add_argument("thread_id")
    resume_command.add_argument("--server", metavar="URL", help="override the configured OpenEngine service")
    approval_list = commands.add_parser("approvals", help="list pending terminal decisions")
    approval_list.add_argument("--server", metavar="URL")
    approval_list.add_argument("--json", action="store_true")
    approve = commands.add_parser("approve", help="approve a pending request")
    approve.add_argument("approval_id")
    approve.add_argument("--server", metavar="URL")
    reject = commands.add_parser("reject", help="reject a pending request")
    reject.add_argument("approval_id")
    reject.add_argument("--reason", required=True)
    reject.add_argument("--server", metavar="URL")
    for name in ("connect", "setup"):
        connection = commands.add_parser(name, help="connect shared source control")
        connection.add_argument("provider", choices=("gh", "github", "gitlab"))
        connection.add_argument("--server", metavar="URL")
        connection.add_argument("--origin", default="https://gitlab.com")
        connection.add_argument("--open", action="store_true")
    repo_command = commands.add_parser("repo", help="select a service repository for new tasks")
    repo_command.add_argument("repository")
    repo_command.add_argument("--server", metavar="URL")
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
    if argv is None and len(sys.argv) == 1 and sys.stdin.isatty() and sys.stdout.isatty():
        return interactive(argparse.Namespace(server=None), load_preferences())
    arguments = parser().parse_args(argv)
    if arguments.command is None:
        parser().print_help()
        return EXIT_USAGE
    preferences = load_preferences()
    if arguments.command == "status":
        return status(arguments, preferences)
    if arguments.command == "doctor":
        return doctor(arguments, preferences)
    if arguments.command == "threads":
        return threads(arguments, preferences)
    if arguments.command == "task":
        return task(arguments, preferences)
    if arguments.command == "run":
        return run(arguments, preferences)
    if arguments.command == "resume":
        return resume(arguments, preferences)
    if arguments.command == "approvals":
        return approvals(arguments, preferences)
    if arguments.command == "approve":
        return decide(arguments, preferences, "accept")
    if arguments.command == "reject":
        return decide(arguments, preferences, "cancel")
    if arguments.command in {"connect", "setup"}:
        return connect(arguments, preferences)
    if arguments.command == "repo":
        return repo(arguments, preferences)
    if arguments.command == "config" and arguments.config_command == "server":
        return configure_server(arguments, preferences)
    if arguments.command == "config" and arguments.config_command == "profile":
        return configure_profile(arguments, preferences)
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())

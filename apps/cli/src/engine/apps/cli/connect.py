"""`engine connect|connections|disconnect`: the service's shared source control and Slack."""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from dataclasses import replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from engine.apps.cli import daemon
from engine.cli import backends

EXIT_OK = 0
EXIT_FAILED = 1
PROVIDERS = ("gh", "github", "gitlab", "slack")
#: What `engine disconnect` can undo; `gh` is the host's own login (`gh auth logout`).
DISCONNECTABLE = ("github", "gitlab", "slack")
#: `/api/source-control/provider` values, by the provider name a command takes.
SOURCE_CONTROL = {"gh": "gh-cli", "github": "github-oauth", "gitlab": "gitlab-oauth"}

#: How long a connect request may take. The service reads its client ID from,
#: and saves the token to, the OS keychain, which can stop to ask for the
#: login password.
KEYCHAIN_TIMEOUT = 60.0
KEYCHAIN_EXPLAINED = (
    "OpenEngine keeps the access token in your system keychain, so it is stored "
    "encrypted rather than in a file and WorkOrders can push and open pull requests "
    "without asking you again. Your system may ask for your login password to allow it."
)
SLACK_TIMEOUT = 120.0


def add_parser(commands: argparse._SubParsersAction) -> None:
    connection = commands.add_parser("connect", help="connect shared source control or Slack")
    connection.add_argument("provider", choices=PROVIDERS)
    connection.add_argument("--server", metavar="URL", help="override the selected backend's URL")
    connection.add_argument("--backend", metavar="NAME", help="the backend to connect (default: the selected one)")
    connection.add_argument("--origin", default="https://gitlab.com", help="the GitLab instance to connect")
    connection.add_argument("--open", action="store_true", help="open the authorization page in a browser")


def add_parsers(commands: argparse._SubParsersAction) -> None:
    add_parser(commands)
    listing = commands.add_parser("connections", help="show source control and Slack connections")
    listing.add_argument("--server", metavar="URL", help="override the selected backend's URL")
    listing.add_argument("--backend", metavar="NAME", help="the backend to ask (default: the selected one)")
    listing.add_argument("--origin", default="https://gitlab.com", help="the GitLab instance to report")
    listing.add_argument("--pretty", action="store_true", help="human-readable output instead of JSON")
    removing = commands.add_parser("disconnect", help="disconnect shared source control or Slack")
    removing.add_argument("provider", choices=DISCONNECTABLE)
    removing.add_argument("--server", metavar="URL", help="override the selected backend's URL")
    removing.add_argument("--backend", metavar="NAME", help="the backend to disconnect (default: the selected one)")
    removing.add_argument("--origin", default="https://gitlab.com", help="the GitLab instance to disconnect")


def selected_backend(arguments: argparse.Namespace) -> backends.Backend:
    backend = backends.load().selected(getattr(arguments, "backend", None))
    if getattr(arguments, "server", None):
        backend = replace(backend, url=backends.normalize_url(arguments.server))
    return backend


def ensure_ready(backend: backends.Backend) -> None:
    """Fail unless the service answers, starting the registered local daemon if it is down."""
    state, _body, detail = daemon.check_health(backend.url)
    if state == "down" and backend.is_local and daemon.read_record() is not None:
        daemon.start_service()
        state, _body, detail = daemon.check_health(backend.url)
    if state != "ready":
        raise RuntimeError(detail)


def request(backend: backends.Backend, path: str, body: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    """GET, or POST `body`, to the service's own (unversioned) `/api` routes."""
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token := backend.token():
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urlopen(Request(f"{backend.url}{path}", data=data, method="GET" if body is None else "POST", headers=headers), timeout=timeout) as response:
            payload = json.loads(response.read() or b"{}")
    except HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error")
        except (OSError, ValueError, AttributeError):
            detail = None
        raise RuntimeError(str(detail or f"server returned HTTP {error.code}")) from None
    except (URLError, TimeoutError, OSError, ValueError) as error:
        raise RuntimeError(f"could not reach {path}: {error}") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} did not return a JSON object")
    return payload


def open_in_browser(url: str) -> None:
    """Open a link the backend sent, only if it is a web page: a spoofed backend must not launch file:// or other handlers."""
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise RuntimeError(f"refusing to open a non-http(s) link from the backend: {url}")
    webbrowser.open(url)


def connect_slack(backend: backends.Backend, arguments: argparse.Namespace) -> None:
    flow = request(backend, "/api/slack/connect", {})
    authorization_url = str(flow["authorizationUrl"])
    print(f"Open {authorization_url} to connect Slack.")
    if arguments.open:
        open_in_browser(authorization_url)
    deadline = time.monotonic() + SLACK_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(1.0)
        if request(backend, "/api/slack/status").get("connected") is True:
            return
    raise RuntimeError("Slack authorization timed out")


def connect_device_flow(backend: backends.Backend, arguments: argparse.Namespace) -> None:
    """GitHub's or GitLab's device flow, then select it as the source-control provider."""
    provider = arguments.provider
    body = {} if provider == "github" else {"origin": arguments.origin}
    print(KEYCHAIN_EXPLAINED)
    flow = request(backend, f"/api/{provider}/connect", body, timeout=KEYCHAIN_TIMEOUT)
    print(f"Open {flow['verificationUri']} and enter code: {flow['userCode']}")
    if arguments.open:
        open_in_browser(str(flow["verificationUri"]))
    while True:
        time.sleep(float(flow.get("interval", 5)))
        result = request(backend, f"/api/{provider}/connect/poll", body, timeout=KEYCHAIN_TIMEOUT)
        if result.get("status") == "complete":
            request(backend, "/api/source-control/provider", {"provider": SOURCE_CONTROL[provider], **body})
            return
        print("Waiting for authorization…")


def connection_rows(backend: backends.Backend, origin: str) -> list[dict[str, Any]]:
    """One row per connection, and which one WorkOrders reach source control through."""
    source = request(backend, "/api/source-control/status")
    github = request(backend, "/api/github/status")
    gitlab = request(backend, f"/api/gitlab/status?{urlencode({'origin': origin})}")
    slack = request(backend, "/api/slack/status")
    gh = source.get("ghCli") or {}
    active = source.get("provider")
    return [
        {"name": "gh", "connected": gh.get("authenticated") is True, "active": active == SOURCE_CONTROL["gh"],
         "detail": gh.get("account") or gh.get("message") or ""},
        {"name": "github", "connected": github.get("connected") is True, "active": active == SOURCE_CONTROL["github"],
         "detail": "" if github.get("clientIdConfigured", True) else "no OAuth client configured"},
        {"name": "gitlab", "connected": gitlab.get("connected") is True, "active": active == SOURCE_CONTROL["gitlab"],
         "detail": gitlab.get("origin", origin)},
        {"name": "slack", "connected": slack.get("connected") is True, "active": False,
         "detail": "events not ready" if slack.get("connected") is True and slack.get("events") is False else ""},
    ]


def connections(arguments: argparse.Namespace) -> int:
    try:
        backend = selected_backend(arguments)
        ensure_ready(backend)
        rows = connection_rows(backend, arguments.origin)
    except (ValueError, RuntimeError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_FAILED
    if not arguments.pretty:
        print(json.dumps({"connections": rows}))
        return EXIT_OK
    for row in rows:
        state = "connected" if row["connected"] else "not connected"
        mark = "*" if row["active"] else " "
        print(f"{mark} {row['name']:<7} {state}{'  ' + row['detail'] if row['detail'] else ''}")
    return EXIT_OK


def disconnect(arguments: argparse.Namespace) -> int:
    try:
        backend = selected_backend(arguments)
        ensure_ready(backend)
        body = {"origin": arguments.origin} if arguments.provider == "gitlab" else {}
        request(backend, f"/api/{arguments.provider}/disconnect", body)
    except (ValueError, RuntimeError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_FAILED
    print(f"Disconnected {arguments.provider}.")
    return EXIT_OK


def main(arguments: argparse.Namespace) -> int:
    try:
        backend = selected_backend(arguments)
        ensure_ready(backend)
        if arguments.provider == "gh":
            request(backend, "/api/source-control/provider", {"provider": SOURCE_CONTROL["gh"]})
            print("GitHub CLI selected. Run `gh auth login` if needed.")
            return EXIT_OK
        if arguments.provider == "slack":
            connect_slack(backend, arguments)
        else:
            connect_device_flow(backend, arguments)
        print("Connected.")
        return EXIT_OK
    except (ValueError, RuntimeError, KeyError) as error:
        print(f"engine: {error}", file=sys.stderr)
        return EXIT_FAILED

"""`engine connect`: connect the service's shared source control or Slack."""

from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from dataclasses import replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from engine.apps.cli import daemon
from engine.cli import backends

EXIT_OK = 0
EXIT_FAILED = 1
PROVIDERS = ("gh", "github", "gitlab", "slack")

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


def connect_slack(backend: backends.Backend, arguments: argparse.Namespace) -> None:
    flow = request(backend, "/api/slack/connect", {})
    authorization_url = str(flow["authorizationUrl"])
    print(f"Open {authorization_url} to connect Slack.")
    if arguments.open:
        webbrowser.open(authorization_url)
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
        webbrowser.open(str(flow["verificationUri"]))
    while True:
        time.sleep(float(flow.get("interval", 5)))
        result = request(backend, f"/api/{provider}/connect/poll", body, timeout=KEYCHAIN_TIMEOUT)
        if result.get("status") == "complete":
            request(backend, "/api/source-control/provider", {"provider": f"{provider}-oauth", **body})
            return
        print("Waiting for authorization…")


def main(arguments: argparse.Namespace) -> int:
    try:
        backend = selected_backend(arguments)
        ensure_ready(backend)
        if arguments.provider == "gh":
            request(backend, "/api/source-control/provider", {"provider": "gh-cli"})
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

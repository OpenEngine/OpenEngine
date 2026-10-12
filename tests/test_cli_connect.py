"""`engine connect`."""

from __future__ import annotations

import argparse

import pytest

from engine.apps.cli import __main__ as cli, connect
from engine.cli.backends import Backend


def arguments(provider: str, **overrides) -> argparse.Namespace:
    return argparse.Namespace(**{"provider": provider, "server": None, "backend": None, "origin": "https://gitlab.com", "open": False, **overrides})


def ready(monkeypatch) -> None:
    monkeypatch.setattr(connect, "selected_backend", lambda _arguments: Backend("local", "http://engine.test"))
    monkeypatch.setattr(connect.daemon, "check_health", lambda _url: ("ready", {}, "OpenEngine is ready"))


def test_connect_is_a_top_level_command(monkeypatch):
    called = []
    monkeypatch.setattr(connect, "main", lambda parsed: called.append(parsed.provider) or 0)

    assert cli.main(["connect", "github"]) == 0
    assert called == ["github"]


def test_connect_slack_opens_the_authorization_url_and_waits_for_connection(monkeypatch, capsys):
    ready(monkeypatch)
    monkeypatch.setattr(connect.time, "sleep", lambda _seconds: None)

    def request(_backend, path, body=None, timeout=10.0):
        return {"authorizationUrl": "https://slack.example/oauth"} if path == "/api/slack/connect" else {"connected": True}

    monkeypatch.setattr(connect, "request", request)
    opened = []
    monkeypatch.setattr(connect.webbrowser, "open", opened.append)

    assert connect.main(arguments("slack", open=True)) == 0

    assert opened == ["https://slack.example/oauth"]
    assert "Connected." in capsys.readouterr().out


@pytest.mark.parametrize("link", ["file:///etc/passwd", "vscode://open?x=1", "javascript:alert(1)"])
def test_connect_refuses_to_open_a_link_that_is_not_a_web_page(link, monkeypatch, capsys):
    ready(monkeypatch)
    monkeypatch.setattr(connect, "request", lambda _backend, path, body=None, timeout=10.0: {
        "authorizationUrl": link, "verificationUri": link, "userCode": "CODE", "interval": 0,
    })
    opened = []
    monkeypatch.setattr(connect.webbrowser, "open", opened.append)

    assert connect.main(arguments("slack", open=True)) == 1
    assert connect.main(arguments("github", open=True)) == 1

    assert opened == []
    assert "non-http(s)" in capsys.readouterr().err


def test_connect_github_explains_the_keychain_and_selects_github_oauth(monkeypatch, capsys):
    ready(monkeypatch)
    requests = []

    def request(_backend, path, body=None, timeout=10.0):
        requests.append((path, body, timeout))
        if path == "/api/github/connect":
            return {"verificationUri": "https://github.com/login/device", "userCode": "CODE", "interval": 0}
        return {"status": "complete"} if path.endswith("/poll") else {}

    monkeypatch.setattr(connect, "request", request)

    assert connect.main(arguments("github")) == 0

    assert requests == [
        ("/api/github/connect", {}, 60.0),
        ("/api/github/connect/poll", {}, 60.0),
        ("/api/source-control/provider", {"provider": "github-oauth"}, 10.0),
    ]
    out = capsys.readouterr().out
    assert "system keychain" in out and "login password" in out and "CODE" in out


def test_connect_reports_an_unreachable_service(monkeypatch, capsys):
    monkeypatch.setattr(connect, "selected_backend", lambda _arguments: Backend("mini", "http://mini.test"))
    monkeypatch.setattr(connect.daemon, "check_health", lambda _url: ("down", None, "cannot reach http://mini.test"))

    assert connect.main(arguments("gh")) == 1
    assert "cannot reach http://mini.test" in capsys.readouterr().err


def test_connections_mark_the_active_provider(monkeypatch, capsys):
    ready(monkeypatch)
    answers = {
        "/api/source-control/status": {"provider": "github-oauth", "ghCli": {"authenticated": True, "account": "octo"}},
        "/api/github/status": {"connected": True, "clientIdConfigured": True},
        "/api/gitlab/status?origin=https%3A%2F%2Fgitlab.com": {"origin": "https://gitlab.com", "connected": False},
        "/api/slack/status": {"configured": True, "connected": True, "events": False},
    }
    monkeypatch.setattr(connect, "request", lambda _backend, path, body=None, timeout=10.0: answers[path])

    assert cli.main(["connections"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "  gh      connected  octo",
        "* github  connected",
        "  gitlab  not connected  https://gitlab.com",
        "  slack   connected  events not ready",
    ]


def test_disconnect_gitlab_names_its_origin(monkeypatch, capsys):
    ready(monkeypatch)
    sent = []
    monkeypatch.setattr(connect, "request", lambda _backend, path, body=None, timeout=10.0: sent.append((path, body)) or {})

    assert cli.main(["disconnect", "gitlab", "--origin", "https://gitlab.example"]) == 0
    assert sent == [("/api/gitlab/disconnect", {"origin": "https://gitlab.example"})]
    assert "Disconnected gitlab." in capsys.readouterr().out

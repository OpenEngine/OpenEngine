"""Browser login verifies identity without issuing sessions or repo credentials."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from engine.apps.web.github_login import GitHubLogin, GitHubLoginConfig


@pytest.fixture
def flow():
    return GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback"
    ))


def browser(flow):
    return TestClient(Starlette(routes=flow.routes()), base_url="https://engine.test")


def start(client):
    response = client.get("/api/auth/github/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=600"))
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["scope"] == ["read:user"]
    assert params["client_id"] == ["login-client"]
    assert params["code_challenge_method"] == ["S256"]
    return params


def callback(client, state, **params):
    return client.get("/api/auth/github/callback", params={"state": state, **params})


def test_success_and_replay(flow):
    client = browser(flow)
    params = start(client)
    calls = []

    def provider(request):
        calls.append(request)
        if request.url.path == "/login/oauth/access_token":
            data = parse_qs(request.content.decode())
            assert data["client_secret"] == ["login-secret"]
            assert data["redirect_uri"] == params["redirect_uri"]
            assert data["code"] == ["one-use-code"]
            digest = hashlib.sha256(data["code_verifier"][0].encode()).digest()
            assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == params["code_challenge"][0]
            return httpx.Response(200, json={"access_token": "private-token"})
        assert str(request.url) == "https://api.github.com/user"
        assert request.headers["Authorization"] == "Bearer private-token"
        return httpx.Response(200, json={"id": 42, "login": "alice", "email": "private"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, params["state"][0], code="one-use-code")
    assert response.json() == {"user": {"id": 42, "login": "alice"}}
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert len(calls) == 2
    assert callback(client, params["state"][0], code="one-use-code").status_code == 400


@pytest.mark.parametrize("failure", ["missing", "wrong", "other-browser", "expired", "denied", "no-code"])
def test_rejects_invalid_callbacks_before_exchange(flow, failure):
    client = browser(flow)
    state = start(client)["state"][0]
    params = {"code": "code"}
    if failure == "missing":
        state = ""
    elif failure == "wrong":
        state = "wrong"
    elif failure == "other-browser":
        client = browser(flow)
    elif failure == "expired":
        nonce, verifier, _ = flow._pending[state]
        flow._pending[state] = (nonce, verifier, 0)
    elif failure == "denied":
        params = {"error": "access_denied"}
    elif failure == "no-code":
        params = {}
    with patch("engine.apps.web.github_login.httpx.AsyncClient") as outbound:
        assert callback(client, state, **params).status_code == 400
        outbound.assert_not_called()


@pytest.mark.parametrize("failure", ["network", "http", "json", "error", "missing-token", "array", "bad-user", "user-http"])
def test_provider_failures_are_sanitized(flow, failure):
    client = browser(flow)
    state = start(client)["state"][0]

    def provider(request):
        if failure == "network":
            raise httpx.ConnectError("secret", request=request)
        if failure == "http":
            return httpx.Response(500, text="secret")
        if failure == "json":
            return httpx.Response(200, text="secret")
        if failure == "error":
            return httpx.Response(200, json={"error": "secret"})
        if failure == "missing-token":
            return httpx.Response(200, json={})
        if failure == "array":
            return httpx.Response(200, json=[])
        if request.url.path == "/user":
            return httpx.Response(403 if failure == "user-http" else 200, json={"id": "42", "login": "alice"})
        return httpx.Response(200, json={"access_token": "secret"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, state, code="code")
    assert response.status_code == 502
    assert response.json() == {"error": "Could not verify GitHub identity"}
    assert state not in flow._pending


def test_disabled():
    client = browser(GitHubLogin(None))
    assert client.get("/api/auth/github/login").status_code == 503
    assert callback(client, "state", code="code").status_code == 503


@pytest.mark.parametrize("uri", ["http://public.test/api/auth/github/callback", "https://engine.test/wrong", "https://engine.test/api/auth/github/callback?next=evil", "https://user@engine.test/api/auth/github/callback"])
def test_rejects_unsafe_configuration(uri):
    with pytest.raises(ValueError):
        GitHubLoginConfig("client", "secret", uri)


def test_loopback_and_secret_repr():
    config = GitHubLoginConfig("client", "private-secret", "http://127.0.0.1:8000/api/auth/github/callback")
    assert "private-secret" not in repr(config)


def test_pending_logins_bounded_and_expired_entries_pruned(flow):
    client = browser(flow)
    for _ in range(1024):
        assert client.get("/api/auth/github/login", follow_redirects=False).status_code == 302
    assert client.get("/api/auth/github/login").status_code == 503
    flow._pending = {k: (v[0], v[1], 0) for k, v in flow._pending.items()}
    assert client.get("/api/auth/github/login", follow_redirects=False).status_code == 302
    assert len(flow._pending) == 1


def test_stale_callback_preserves_newer_login(flow):
    client = browser(flow)
    older = start(client)["state"][0]
    newer = start(client)["state"][0]
    response = callback(client, older, code="old")
    assert response.status_code == 400
    assert "set-cookie" not in response.headers
    # Refreshing a consumed callback must also leave the new cookie alone.
    assert "set-cookie" not in callback(client, older, code="old").headers

    def provider(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": 42, "login": "alice"})
        return httpx.Response(200, json={"access_token": "token"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, newer, code="new")
    assert response.status_code == 200
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_token_exchange_uses_rotated_file_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", raising=False)
    secret_file = tmp_path / ".env"
    secret_file.write_text("ENGINE_GITHUB_LOGIN_CLIENT_SECRET=initial\n")
    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "initial", "https://engine.test/api/auth/github/callback", secret_file
    ))
    client = browser(flow)
    state = start(client)["state"][0]
    secret_file.write_text("ENGINE_GITHUB_LOGIN_CLIENT_SECRET=rotated\n")

    def provider(request):
        if request.url.path == "/login/oauth/access_token":
            assert parse_qs(request.content.decode())["client_secret"] == ["rotated"]
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={"id": 42, "login": "alice"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        assert callback(client, state, code="code").status_code == 200

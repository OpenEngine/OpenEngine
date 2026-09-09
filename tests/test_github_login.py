"""Browser login issues session cookies after GitHub identity verification."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount, Route
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
    return client.get("/api/auth/github/callback", params={"state": state, **params},
                      follow_redirects=False)


def _mock_provider(success_id=42, success_login="alice"):
    def provider(request):
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "private-token"})
        return httpx.Response(200, json={"id": success_id, "login": success_login, "email": "private"})
    return provider


def test_success_issues_session_and_redirects(flow):
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
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert response.headers["referrer-policy"] == "no-referrer"
    # Login cookie is cleared.
    cookies = response.headers.get_list("set-cookie")
    login_cookie = [c for c in cookies if "engine_github_login" in c]
    assert login_cookie and "Max-Age=0" in login_cookie[0]
    # Session cookie is set.
    session_cookie = [c for c in cookies if "engine_session" in c]
    assert session_cookie
    assert len(calls) == 2
    # Replay with consumed cookie is rejected.
    replay = callback(client, params["state"][0], code="one-use-code")
    assert replay.status_code == 302
    assert "error=" in replay.headers["location"]


@pytest.mark.parametrize("failure", ["missing", "wrong", "other-browser", "expired", "denied", "no-code"])
def test_rejects_invalid_callbacks_before_exchange(flow, failure, monkeypatch):
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
        monkeypatch.setattr("engine.apps.web.github_login.time.time", lambda: 10**12)
    elif failure == "denied":
        params = {"error": "access_denied"}
    elif failure == "no-code":
        params = {}
    with patch("engine.apps.web.github_login.httpx.AsyncClient") as outbound:
        response = callback(client, state, **params)
        assert response.status_code == 302
        assert "/login?error=" in response.headers["location"]
        outbound.assert_not_called()


@pytest.mark.parametrize("failure", ["network", "http", "json", "error", "missing-token", "array", "bad-user", "user-http"])
def test_provider_failures_redirect_with_error(flow, failure):
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
    assert response.status_code == 302
    assert "/login?error=failed" in response.headers["location"]
    # Replay with consumed cookie is rejected.
    replay = callback(client, state, code="code")
    assert replay.status_code == 302
    assert "error=" in replay.headers["location"]


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


def test_flood_does_not_block_independent_browser(flow):
    client = browser(flow)
    independent = browser(flow)
    state = start(independent)["state"][0]
    for _ in range(1100):
        client.cookies.clear()
        assert client.get("/api/auth/github/login", follow_redirects=False).status_code == 302
    assert browser(flow).get("/api/auth/github/login", follow_redirects=False).status_code == 302

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        assert callback(independent, state, code="code").status_code == 302


@pytest.mark.parametrize("cookie", ["garbage", "a.b.c.d", "x" * 257])
def test_invalid_cookie_rejected(flow, cookie):
    client = browser(flow)
    state = start(client)["state"][0]
    client.cookies.clear()
    with patch("engine.apps.web.github_login.httpx.AsyncClient") as outbound:
        response = client.get("/api/auth/github/callback", params={"state": state, "code": "code"},
                              headers={"cookie": f"engine_github_login={cookie}"},
                              follow_redirects=False)
        assert response.status_code == 302
        assert "error=" in response.headers["location"]
        outbound.assert_not_called()


def test_signed_cookie_tampering_rejected(flow):
    client = browser(flow)
    state = start(client)["state"][0]
    cookie = client.cookies.get("engine_github_login")
    parts = cookie.split(".")
    parts[2] = str(10**12)
    client.cookies.clear()
    with patch("engine.apps.web.github_login.httpx.AsyncClient") as outbound:
        response = client.get("/api/auth/github/callback", params={"state": state, "code": "code"},
                              headers={"cookie": "engine_github_login=" + ".".join(parts)},
                              follow_redirects=False)
        assert response.status_code == 302
        assert "error=" in response.headers["location"]
        outbound.assert_not_called()


def test_stale_callback_preserves_newer_login(flow):
    client = browser(flow)
    older = start(client)["state"][0]
    newer = start(client)["state"][0]
    response = callback(client, older, code="old")
    assert response.status_code == 302
    assert "error=" in response.headers["location"]
    # The login cookie for the newer flow should not be cleared.
    cookies = response.headers.get_list("set-cookie")
    login_cleared = [c for c in cookies if "engine_github_login" in c and "Max-Age=0" in c]
    assert not login_cleared
    # Refreshing a consumed callback must also leave the new cookie alone.
    replay_cookies = callback(client, older, code="old").headers.get_list("set-cookie")
    assert not [c for c in replay_cookies if "engine_github_login" in c and "Max-Age=0" in c]

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, newer, code="new")
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    cookies = response.headers.get_list("set-cookie")
    login_cleared = [c for c in cookies if "engine_github_login" in c and "Max-Age=0" in c]
    assert login_cleared


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
        assert callback(client, state, code="code").status_code == 302


def test_captured_callback_replay_cannot_verify_identity_twice(flow):
    client = browser(flow)
    state = start(client)["state"][0]
    cookie = client.cookies.get("engine_github_login")
    exchanges = 0
    identities = 0

    def provider(request):
        nonlocal exchanges, identities
        if request.url.path == "/login/oauth/access_token":
            exchanges += 1
            if exchanges > 1:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "token"})
        identities += 1
        return httpx.Response(200, json={"id": 42, "login": "alice"})

    for expected in (302, 302):
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
            response = client.get(
                "/api/auth/github/callback", params={"state": state, "code": "same-code"},
                headers={"cookie": f"engine_github_login={cookie}"},
                follow_redirects=False,
            )
        assert response.status_code == expected
    assert identities == 1


def test_status_unauthenticated(flow):
    client = browser(flow)
    response = client.get("/api/auth/github/status")
    assert response.json() == {"authenticated": False, "user": None, "loginRequired": True}


def test_status_after_login(flow):
    client = browser(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        callback(client, params["state"][0], code="code")
    response = client.get("/api/auth/github/status")
    data = response.json()
    assert data["authenticated"] is True
    assert data["user"]["id"] == 42
    assert data["user"]["login"] == "alice"


def test_logout_clears_session(flow):
    client = browser(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        callback(client, params["state"][0], code="code")
    assert client.get("/api/auth/github/status").json()["authenticated"] is True
    response = client.post("/api/auth/github/logout")
    assert response.json() == {"ok": True}
    assert client.get("/api/auth/github/status").json()["authenticated"] is False


def test_logout_without_session_is_noop(flow):
    """Cross-site POST without a session cookie does not set Set-Cookie."""
    client = browser(flow)
    response = client.post("/api/auth/github/logout")
    assert response.json() == {"ok": True}
    assert "set-cookie" not in response.headers


def test_status_not_configured():
    flow = GitHubLogin(None)
    client = browser(flow)
    response = client.get("/api/auth/github/status")
    assert response.json() == {"authenticated": False, "user": None, "loginRequired": False}


# --- middleware tests ---------------------------------------------------------

def _app_with_middleware(flow):
    """Mount the real graph API alongside a web route behind session auth."""
    from engine.graph_runtime.api import create_app
    from graph_runtime_fakes import ScriptedGraphRuntime
    from starlette.responses import JSONResponse as _J
    routes = flow.routes() + [
        Route("/api/data", lambda _r: _J({"ok": True})),
        Mount("/graph", app=create_app(ScriptedGraphRuntime())),
    ]
    inner = Starlette(routes=routes)
    app = flow.middleware(inner)
    return TestClient(app, base_url="https://engine.test")


@pytest.mark.parametrize("path", ["/api/data", "/graph/api/graphs"])
def test_middleware_blocks_unauthenticated_api(flow, path):
    client = _app_with_middleware(flow)
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["error"] == "authentication required"


def test_middleware_allows_auth_endpoints(flow):
    client = _app_with_middleware(flow)
    # Status is exempt from the middleware.
    response = client.get("/api/auth/github/status")
    assert response.status_code == 200
    assert response.json()["loginRequired"] is True


@pytest.mark.parametrize("path, expected", [
    ("/api/data", {"ok": True}), ("/graph/api/graphs", {"graphs": []}),
])
def test_middleware_allows_authenticated_api(flow, path, expected):
    client = _app_with_middleware(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        callback(client, params["state"][0], code="code")
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == expected


def test_middleware_noop_when_not_configured():
    flow = GitHubLogin(None)
    from starlette.responses import JSONResponse as _J
    routes = flow.routes() + [Route("/api/data", lambda _r: _J({"ok": True}))]
    inner = Starlette(routes=routes)
    app = flow.middleware(inner)
    client = TestClient(app, base_url="https://engine.test")
    response = client.get("/api/data")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("failure", ["expired", "malformed", "tampered", "other-key"])
def test_invalid_session_rejected_at_status_and_api_boundaries(flow, failure):
    if failure == "expired":
        with patch("engine.apps.web.github_login.time.time", return_value=0):
            cookie = flow._make_session_cookie(42, "alice")
    elif failure == "malformed":
        cookie = "not-a-session"
    elif failure == "tampered":
        cookie = flow._make_session_cookie(42, "alice").replace("42|alice|", "43|admin|")
    else:
        cookie = GitHubLogin(flow.config)._make_session_cookie(42, "alice")
    client = _app_with_middleware(flow)
    headers = {"cookie": f"engine_session={cookie}"}
    status = client.get("/api/auth/github/status", headers=headers)
    assert status.status_code == 200
    assert status.json() == {"authenticated": False, "user": None, "loginRequired": True}
    for path in ("/api/data", "/graph/api/graphs"):
        response = client.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"error": "authentication required"}


@pytest.mark.parametrize("destination, expected", [
    ("/runs/run-123?tab=events#latest", "/runs/run-123?tab=events#latest"),
    ("/conversations/thread-1", "/conversations/thread-1"),
    ("/projects/project%20with%20spaces/milestones?q=two%20words", "/projects/project%20with%20spaces/milestones?q=two%20words"),
    ("https://evil.test/", "/"), ("//evil.test/", "/"),
    ("/%2fevil.test", "/"), ("/\\evil.test", "/"),
    ("/%0d%0aLocation:evil", "/"), ("javascript:alert(1)", "/"),
])
def test_login_returns_to_validated_destination(flow, destination, expected):
    client = browser(flow)
    response = client.get("/api/auth/github/login", params={"return_to": destination},
                          follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, state, code="code", return_to="//evil.test")
    assert response.headers["location"] == expected
    assert client.get("/api/auth/github/status").json()["authenticated"] is True

@pytest.mark.parametrize("error", ["failed", "denied", "expired"])
def test_login_retry_preserves_destination(flow, error):
    client = browser(flow)
    destination = "/runs/run-123?tab=events#latest"
    response = client.get("/api/auth/github/login", params={"return_to": destination}, follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    if error == "failed":
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
            response = callback(client, state, code="bad")
    elif error == "expired":
        cookie = client.cookies.get("engine_github_login")
        with patch("engine.apps.web.github_login.time.time", return_value=10**12):
            response = client.get(
                "/api/auth/github/callback", params={"state": state, "code": "old"},
                headers={"cookie": f"engine_github_login={cookie}"}, follow_redirects=False,
            )
    else:
        response = callback(client, state, error="access_denied")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query == {"error": [error], "return_to": [destination]}
    response = client.get("/api/auth/github/login", params={"return_to": query["return_to"][0]}, follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, state, code="good")
    assert response.headers["location"] == destination

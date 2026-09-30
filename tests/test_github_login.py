"""Browser login issues session cookies after GitHub identity verification."""

import base64
import hashlib
import time
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
    config = GitHubLoginConfig("client", "private-secret", "http://127.0.0.1:4364/api/auth/github/callback")
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


# --- service token ------------------------------------------------------------

SERVICE_TOKEN = "service-token-" * 3


def _service_app(token=SERVICE_TOKEN):
    from starlette.responses import JSONResponse as _J
    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback"
    ), lambda: token)
    routes = flow.routes() + [
        Route("/api/runs", lambda _r: _J({"runId": "run-1"}, status_code=201), methods=["GET", "POST"]),
        Route("/api/runs/{run_id}", lambda _r: _J({"ok": True}), methods=["POST"]),
        Route("/api/data", lambda _r: _J({"ok": True}), methods=["GET", "POST"]),
    ]
    return TestClient(flow.middleware(Starlette(routes=routes)), base_url="https://engine.test")


def test_service_token_admits_run_creation():
    client = _service_app()
    response = client.post("/api/runs", json={}, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 201


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/runs"), ("POST", "/api/runs/run-1"), ("POST", "/api/data"), ("GET", "/api/data"),
])
def test_service_token_admits_nothing_else(method, path):
    client = _service_app()
    response = client.request(method, path, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 401


@pytest.mark.parametrize("authorization", [
    None, "Bearer wrong-" + SERVICE_TOKEN, SERVICE_TOKEN, f"Basic {SERVICE_TOKEN}",
])
def test_service_token_rejects_missing_or_wrong_header(authorization):
    client = _service_app()
    headers = {"Authorization": authorization} if authorization else {}
    assert client.post("/api/runs", json={}, headers=headers).status_code == 401


@pytest.mark.parametrize("configured", ["", "short", "has whitespace " * 3])
def test_unset_or_invalid_service_token_admits_nothing(configured):
    client = _service_app(configured)
    response = client.post("/api/runs", json={}, headers={"Authorization": f"Bearer {configured}"})
    assert response.status_code == 401


def test_service_token_rotation_takes_effect_per_request():
    current = {"token": SERVICE_TOKEN}
    from starlette.responses import JSONResponse as _J
    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback"
    ), lambda: current["token"])
    routes = [Route("/api/runs", lambda _r: _J({}, status_code=201), methods=["POST"])]
    client = TestClient(flow.middleware(Starlette(routes=routes)), base_url="https://engine.test")
    assert client.post("/api/runs", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}).status_code == 201
    current["token"] = "rotated-token-" * 3
    assert client.post("/api/runs", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}).status_code == 401
    assert client.post("/api/runs", headers={"Authorization": f"Bearer {current['token']}"}).status_code == 201


def test_service_token_reader_prefers_environment_and_rereads_file(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine.apps.web.__main__ import _service_token_reader

    monkeypatch.delenv("ENGINE_SERVICE_TOKEN", raising=False)
    loaded = SimpleNamespace(path=tmp_path / "engine.toml")
    env = tmp_path / ".env"
    env.write_text(f"ENGINE_SERVICE_TOKEN={SERVICE_TOKEN}\n")
    read = _service_token_reader(loaded)
    assert read() == SERVICE_TOKEN
    env.write_text("ENGINE_SERVICE_TOKEN=rotated-token-rotated-token-rotated\n")
    assert read() == "rotated-token-rotated-token-rotated"
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "from-environment-" * 2)
    assert read() == "from-environment-" * 2


def test_invalid_service_token_fails_startup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine.apps.web.__main__ import _service_token_reader
    from engine.runtime import EngineConfigError

    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "short")
    with pytest.raises(EngineConfigError, match="ENGINE_SERVICE_TOKEN"):
        _service_token_reader(SimpleNamespace(path=tmp_path / "engine.toml"))


@pytest.mark.parametrize(("answer", "error"), [
    (False, "forbidden"),
    (RuntimeError("GitHub is down"), "unverified"),
])
def test_a_login_without_repository_write_access_gets_no_session(answer, error):
    """Signing in proves who someone is, not that they may see WorkOrders:
    only accounts that can write to the repository get a session, and access
    that cannot be confirmed is refused."""
    asked = []

    async def authorize(user_id, login):
        asked.append((user_id, login))
        if isinstance(answer, Exception):
            raise answer
        return {"acme/api": answer}

    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback"
    ), authorize=authorize)
    client = browser(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, params["state"][0], code="code")

    assert asked == [(42, "alice")]
    assert response.status_code == 302
    assert response.headers["location"] == f"/login?error={error}"
    assert not any("engine_session=" in cookie and "Max-Age=86400" in cookie
                   for cookie in response.headers.get_list("set-cookie"))


def test_a_login_with_repository_write_access_gets_a_session():
    async def authorize(user_id, login):
        return {"acme/api": (user_id, login) == (42, "alice")}

    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback"
    ), authorize=authorize)
    client = browser(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, params["state"][0], code="code")

    assert response.headers["location"] == "/"
    assert any(cookie.startswith("engine_session=")
               for cookie in response.headers.get_list("set-cookie"))


def _signed_in(flow):
    """A browser holding alice's session, behind the session middleware."""
    client = _app_with_middleware(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, params["state"][0], code="code")
    assert response.headers["location"] == "/"
    return client


def _login_flow(authorize, operators=()):
    return GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback"
    ), authorize=authorize, operators=operators)


def test_an_operator_signs_in_without_a_repository_check():
    """Operators are let in by GitHub user ID even when the server cannot ask
    GitHub about repository access at all."""
    async def authorize(user_id, login):
        raise RuntimeError("gh auth expired")

    client = _signed_in(_login_flow(authorize, operators={42}))

    assert client.get("/api/data").json() == {"ok": True}
    status = client.get("/api/auth/github/status").json()
    assert status["authenticated"] is True
    assert status["accessCheckFailing"] is False


def test_revoked_access_ends_the_session_once_the_cache_expires(monkeypatch):
    answers = [True, True, False]
    asked = []

    async def authorize(user_id, login):
        asked.append((user_id, login))
        return {"acme/api": answers.pop(0)}

    now = [1000.0]
    monkeypatch.setattr("engine.apps.web.github_login.time.monotonic", lambda: now[0])
    client = _signed_in(_login_flow(authorize))

    # Within the cache lifetime GitHub is not asked again.
    assert client.get("/api/data").status_code == 200
    assert client.get("/api/auth/github/status").json()["authenticated"] is True
    assert asked == [(42, "alice")]

    now[0] += 301
    assert client.get("/api/data").status_code == 200
    now[0] += 301
    assert client.get("/api/data").status_code == 401
    status = client.get("/api/auth/github/status")
    assert status.json()["authenticated"] is False
    assert "accessCheckFailing" not in status.json()
    assert asked == [(42, "alice")] * 3


def test_a_failed_recheck_is_retried_rather_than_cached(monkeypatch):
    """A temporary `gh` failure refuses the request it happened on, but does not
    lock the user out for the cache lifetime or end their session."""
    answers = [True, RuntimeError("gh is down"), True]

    async def authorize(user_id, login):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"acme/api": answer}

    now = [1000.0]
    monkeypatch.setattr("engine.apps.web.github_login.time.monotonic", lambda: now[0])
    flow = _login_flow(authorize)
    client = _signed_in(flow)
    now[0] += 301

    response = client.get("/api/data")
    assert response.status_code == 503
    assert response.json()["error"] == "repository access could not be verified"
    assert flow.access_check_failing is True
    assert client.get("/api/data").status_code == 200
    assert flow.access_check_failing is False
    assert answers == []


def test_a_failed_recheck_does_not_report_the_session_as_signed_in(monkeypatch):
    answers = [True, RuntimeError("gh is down")]

    async def authorize(user_id, login):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return {"acme/api": answer}

    now = [1000.0]
    monkeypatch.setattr("engine.apps.web.github_login.time.monotonic", lambda: now[0])
    client = _signed_in(_login_flow(authorize))
    now[0] += 301

    status = client.get("/api/auth/github/status")
    assert status.status_code == 503
    assert status.json() == {"error": "repository access could not be verified"}
    # The session is kept, so the next successful check lets the user back in.
    assert not any("engine_session=" in cookie for cookie in status.headers.get_list("set-cookie"))


def test_an_open_stream_ends_when_access_is_revoked(monkeypatch):
    """A long-lived response is rechecked while it streams, not only when it starts."""
    import asyncio

    from starlette.responses import StreamingResponse

    allowed = [True]

    async def authorize(user_id, login):
        return {"acme/api": allowed[0]}

    async def events(_request):
        async def body():
            for n in range(1000):
                if n == 3:
                    allowed[0] = False
                    flow._access.clear()
                yield f"data: {n}\n\n".encode()
                await asyncio.sleep(0.01)
        return StreamingResponse(body(), media_type="text/event-stream")

    monkeypatch.setattr("engine.apps.web.github_login._STREAM_RECHECK", 0.01)
    flow = _login_flow(authorize)
    inner = Starlette(routes=flow.routes() + [Route("/api/events", events)])
    client = TestClient(flow.middleware(inner), base_url="https://engine.test")
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        callback(client, params["state"][0], code="code")

    response = client.get("/api/events")
    assert response.status_code == 200
    assert response.text.startswith("data: 0\n\n")
    assert "data: 999" not in response.text
    assert client.get("/api/events").status_code == 401


def test_a_slow_lookup_holds_up_only_its_own_user():
    import asyncio

    async def scenario():
        release = asyncio.Event()

        async def authorize(user_id, login):
            if login == "slow":
                await release.wait()
            return {"acme/api": True}

        flow = _login_flow(authorize)
        slow = asyncio.create_task(flow.has_access({"id": 1, "login": "slow"}))
        await asyncio.sleep(0)
        assert await asyncio.wait_for(flow.has_access({"id": 2, "login": "fast"}), 1) is True
        assert not slow.done()
        release.set()
        assert await slow is True

    asyncio.run(scenario())


def _fallback_flow(authorize, authorize_user):
    return GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback"
    ), authorize=authorize, authorize_user=authorize_user)


def _sign_in(flow):
    client = _app_with_middleware(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        return client, callback(client, params["state"][0], code="code")


def test_the_users_own_token_admits_them_when_the_servers_lookup_fails():
    """A broken server connection does not lock out the people who can fix it:
    GitHub, asked with the user's own sign-in token, vouches for them."""
    tokens = []

    async def authorize(user_id, login):
        raise RuntimeError("GitHub OAuth provider failed: 401")

    async def authorize_user(token):
        tokens.append(token)
        return {"acme/api": True}

    flow = _fallback_flow(authorize, authorize_user)
    client, response = _sign_in(flow)

    assert response.headers["location"] == "/"
    assert tokens == ["private-token"]
    assert client.get("/api/data").status_code == 200
    # Signed-in users are told the server's connection is failing.
    assert client.get("/api/auth/github/status").json()["accessCheckFailing"] is True
    [(token, _)] = flow._user_tokens[42].values()
    assert token == "private-token"
    client.post("/api/auth/github/logout")
    assert flow._user_tokens == {}


@pytest.mark.parametrize("fallback", [False, RuntimeError("private repository")])
def test_the_users_own_token_that_cannot_confirm_access_admits_nobody(fallback):
    async def authorize(user_id, login):
        raise RuntimeError("GitHub OAuth provider failed: 401")

    async def authorize_user(token):
        if isinstance(fallback, Exception):
            raise fallback
        return {"acme/api": fallback}

    flow = _fallback_flow(authorize, authorize_user)
    _, response = _sign_in(flow)

    assert response.headers["location"] == "/login?error=unverified"
    assert flow._user_tokens == {}


def test_the_servers_answer_outranks_the_users_own_token():
    """The user's token only stands in for a failed lookup; it cannot overturn a no."""
    async def authorize(user_id, login):
        return {"acme/api": False}

    async def authorize_user(token):
        return {"acme/api": True}

    flow = _fallback_flow(authorize, authorize_user)
    _, response = _sign_in(flow)

    assert response.headers["location"] == "/login?error=forbidden"
    assert flow._user_tokens == {}


def test_the_users_own_token_is_not_asked_when_the_server_answers():
    """A routine check costs one lookup; the fallback waits for a failure."""
    import asyncio

    asked = []

    async def authorize(user_id, login):
        return {"acme/api": True}

    async def authorize_user(token):
        asked.append(token)
        return {"acme/api": True}

    flow = _fallback_flow(authorize, authorize_user)
    later = time.time() + 60
    flow._user_tokens[42] = {"laptop": ("a", later), "phone": ("b", later)}

    assert asyncio.run(flow.has_access({"id": 42, "login": "alice"})) is True
    assert asked == []


def test_the_users_tokens_are_asked_one_at_a_time_newest_first():
    import asyncio

    asked, running = [], []

    async def authorize(user_id, login):
        raise RuntimeError("GitHub OAuth provider failed: 401")

    async def authorize_user(token):
        running.append(token)
        assert len(running) == 1
        await asyncio.sleep(0)
        asked.append(token)
        running.remove(token)
        return {"acme/api": token == "old"}

    flow = _fallback_flow(authorize, authorize_user)
    later = time.time() + 60
    flow._user_tokens[42] = {"laptop": ("old", later), "phone": ("new", later)}

    assert asyncio.run(flow.has_access({"id": 42, "login": "alice"})) is True
    assert asked == ["new", "old"]


def test_the_fallback_gets_only_what_is_left_of_the_time_budget():
    """The server's lookup and the fallback share one deadline, so a check stays bounded."""
    import asyncio

    async def authorize(user_id, login):
        await asyncio.sleep(1)
        return {"acme/api": True}

    async def authorize_user(token):
        return {"acme/api": True}

    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback"
    ), authorize=authorize, authorize_user=authorize_user, access_timeout=0.05)
    flow._user_tokens[42] = {"session": ("private-token", time.time() + 60)}

    started = time.monotonic()
    # The server's lookup used the whole budget, so nothing is left to confirm access with.
    assert asyncio.run(flow.has_access({"id": 42, "login": "alice"})) is None
    assert time.monotonic() - started < 0.5


def test_signing_out_keeps_the_fallback_for_the_users_other_browsers():
    """Each session keeps its own sign-in token, so one sign-out does not strand the rest."""
    async def authorize(user_id, login):
        raise RuntimeError("GitHub OAuth provider failed: 401")

    async def authorize_user(token):
        return {"acme/api": True}

    flow = _fallback_flow(authorize, authorize_user)
    laptop, _ = _sign_in(flow)
    phone, _ = _sign_in(flow)
    assert len(flow._user_tokens[42]) == 2

    laptop.post("/api/auth/github/logout")
    flow._access.clear()  # the five-minute recheck comes due

    assert len(flow._user_tokens[42]) == 1
    assert phone.get("/api/data").status_code == 200


def test_a_sign_in_token_is_dropped_when_its_session_expires():
    async def authorize(user_id, login):
        raise RuntimeError("GitHub OAuth provider failed: 401")

    async def authorize_user(token):
        return {"acme/api": True}

    flow = _fallback_flow(authorize, authorize_user)
    _sign_in(flow)
    assert 42 in flow._user_tokens

    with patch("engine.apps.web.github_login.time.time", return_value=time.time() + 86401):
        assert flow._user_tokens_of(42) == []
        flow._drop_expired_user_tokens()
    assert flow._user_tokens == {}


def test_a_sign_in_token_is_dropped_when_access_is_revoked():
    answers = [True, False]

    async def authorize(user_id, login):
        return {"acme/api": answers.pop(0)}

    flow = _fallback_flow(authorize, None)
    client, response = _sign_in(flow)
    assert response.headers["location"] == "/"
    assert 42 in flow._user_tokens

    flow._access.clear()
    assert client.get("/api/data").status_code == 401
    assert flow._user_tokens == {}


@pytest.mark.parametrize("first", [False, RuntimeError("token revoked")])
def test_any_of_the_users_tokens_can_vouch_for_them(first):
    """One browser's revoked or read-only token does not stop another's from admitting the user."""
    import asyncio

    asked = []

    async def authorize(user_id, login):
        raise RuntimeError("GitHub OAuth provider failed: 401")

    async def authorize_user(token):
        asked.append(token)
        if token == "revoked-token":
            if isinstance(first, Exception):
                raise first
            return {"acme/api": first}
        return {"acme/api": True}

    flow = _fallback_flow(authorize, authorize_user)
    later = time.time() + 60
    # Newest first, so the revoked token is asked before the good one.
    flow._user_tokens[42] = {"laptop": ("good-token", later), "phone": ("revoked-token", later)}

    assert asyncio.run(flow.has_access({"id": 42, "login": "alice"})) is True
    assert asked == ["revoked-token", "good-token"]


def test_no_token_that_confirms_access_admits_nobody():
    import asyncio

    async def authorize(user_id, login):
        raise RuntimeError("GitHub OAuth provider failed: 401")

    async def authorize_user(token):
        if token == "a":
            raise RuntimeError("token revoked")
        return {"acme/api": False}

    flow = _fallback_flow(authorize, authorize_user)
    later = time.time() + 60
    flow._user_tokens[42] = {"laptop": ("a", later), "phone": ("b", later)}

    assert asyncio.run(flow.has_access({"id": 42, "login": "alice"})) is None


def test_every_access_check_drops_expired_sign_in_tokens():
    """Expired tokens go on the next check by anyone, even one the server answers."""
    import asyncio

    async def authorize(user_id, login):
        return {"acme/api": True}

    flow = _fallback_flow(authorize, None)
    flow.operators = frozenset({7})
    flow._user_tokens[42] = {"old": ("expired-token", time.time() - 1),
                             "new": ("live-token", time.time() + 60)}
    flow._user_tokens[43] = {"old": ("expired-token", time.time() - 1)}

    assert asyncio.run(flow.has_access({"id": 7, "login": "operator"})) is True
    assert flow._user_tokens == {42: {"new": ("live-token", flow._user_tokens[42]["new"][1])}}


def test_each_repository_is_answered_and_cached_per_user(monkeypatch):
    """A user sees the repositories they can write to, not the first one that
    admitted them, and that answer is kept for the cache lifetime."""
    import asyncio

    asked = []

    async def authorize(user_id, login):
        asked.append(user_id)
        return {"acme/api": True, "acme/web": False, "acme/docs": user_id == 2}

    now = [1000.0]
    monkeypatch.setattr("engine.apps.web.github_login.time.monotonic", lambda: now[0])
    flow = _login_flow(authorize)
    alice, bob = {"id": 1, "login": "alice"}, {"id": 2, "login": "bob"}

    assert asyncio.run(flow.writable_repositories(alice)) == {"acme/api"}
    assert asyncio.run(flow.writable_repositories(bob)) == {"acme/api", "acme/docs"}
    assert asyncio.run(flow.writable_repositories(alice)) == {"acme/api"}
    assert asked == [1, 2]
    now[0] += 301
    asyncio.run(flow.writable_repositories(alice))
    assert asked == [1, 2, 1]


def test_a_failed_repository_is_hidden_and_not_cached():
    """One repository's failed lookup hides that repository for the request
    without refusing the others, and is asked again next time."""
    import asyncio

    answers = [
        {"acme/api": True, "acme/web": None},
        {"acme/api": True, "acme/web": True},
    ]

    async def authorize(user_id, login):
        return answers.pop(0)

    flow = _login_flow(authorize)
    alice = {"id": 1, "login": "alice"}

    assert asyncio.run(flow.writable_repositories(alice)) == {"acme/api"}
    assert asyncio.run(flow.has_access(alice)) is True
    assert asyncio.run(flow.writable_repositories(alice)) == {"acme/api", "acme/web"}
    assert answers == []


def test_access_is_unknown_when_nothing_admits_and_a_lookup_failed():
    import asyncio

    async def authorize(user_id, login):
        return {"acme/api": False, "acme/web": None}

    flow = _login_flow(authorize)

    assert asyncio.run(flow.has_access({"id": 1, "login": "alice"})) is None
    assert flow.access_check_failing is True


def test_operators_and_the_service_token_see_every_repository():
    import asyncio

    from starlette.requests import Request

    async def authorize(user_id, login):
        return {"acme/api": True}

    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback"
    ), service_token=lambda: "s" * 40, authorize=authorize, operators={42})

    def request(method="GET", path="/api/runs", headers=()):
        return Request({"type": "http", "method": method, "path": path, "headers": list(headers)})

    with patch.object(flow, "_read_session", return_value={"id": 42, "login": "alice"}):
        assert asyncio.run(flow.visible_repositories(request())) is None
    with patch.object(flow, "_read_session", return_value={"id": 7, "login": "bob"}):
        assert asyncio.run(flow.visible_repositories(request())) == {"acme/api"}
    service = request("POST", "/api/runs", [(b"authorization", f"Bearer {'s' * 40}".encode())])
    assert asyncio.run(flow.visible_repositories(service)) is None
    assert asyncio.run(GitHubLogin(None).visible_repositories(request())) is None


def test_the_users_own_token_fills_in_only_the_repositories_the_server_could_not_answer():
    """The fallback adds repositories whose lookup failed; the server's no stands."""
    import asyncio

    async def authorize(user_id, login):
        return {"acme/api": False, "acme/web": None, "acme/docs": None}

    async def authorize_user(token):
        return {"acme/api": True, "acme/web": True, "acme/docs": None}

    flow = _fallback_flow(authorize, authorize_user)
    flow._user_tokens[42] = {"laptop": ("token", time.time() + 60)}
    alice = {"id": 42, "login": "alice"}

    assert asyncio.run(flow.writable_repositories(alice)) == {"acme/web"}
    # `acme/docs` is still unknown, so nothing is cached.
    assert 42 not in flow._access
